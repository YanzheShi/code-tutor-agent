"""Chat router — streaming + non-streaming chat endpoints."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from starlette.responses import StreamingResponse

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.mcp.search_client import search_mcp_configured
from code_tutor_agent.observability import build_run_config
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)
router = APIRouter()


def _chunk_text(text: str, size: int = 6):
    """把文本切成小段，用于伪流式输出，保持打字效果。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _sse_payload(text: str, key: str = "t") -> str:
    """把文本包进 JSON 再作为 SSE data 发送（保留换行的同时避免 \n\n 撞上事件边界）。

    LLM token 里常含代码块的换行/空行，若直接 `data: {text}\n\n`，前端的
    `split('\\n\\n')` 会把单个事件拦腰切开、后半段被丢弃；JSON 转义可彻底规避。
    事件形如 `data: {"t": "...\\n..."}\n\n`。
    """
    return f"data: {json.dumps({key: text}, ensure_ascii=False)}\n\n"


def _normalize_to_messages(raw_list) -> list[Message]:
    """把 checkpointer 中可能混入的 dict / 其他类型统一成 Message 列表（防御性）。"""
    out: list[Message] = []
    for m in (raw_list or []):
        if isinstance(m, Message):
            out.append(m)
        elif isinstance(m, dict):
            out.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
        else:
            out.append(Message(role="tutor", content=str(m)))
    return out


def _explicit_generate_signals(message: str) -> bool:
    """用户显式要求立即出题（如"出题" / "来一道" / "给我出"）。

    命中这些关键词时，用户已经表达了"别再追问、直接出题"的意图，
    应无条件推进到出题，而不是被 LLM 意图判定的偶发抖动拦在 dialog 态。
    """
    return any(k in message for k in ("出题", "来一道", "给我出", "出道题", "开始做题"))


def _explicit_random_signals(message: str) -> bool:
    """用户把 topic / 难度都交给 AI 决定（"随机" / "随便" / "由你决定" 等）。"""
    return any(k in message for k in ("随机", "随便", "都行", "由你决定", "你决定", "你来定", "随你"))


def _build_results_context(values: dict) -> str:
    """把用户最近的运行 / 判题结果格式化为文本，供导师 prompt 注入。

    这是修复「导师不看真实运行/判题结果、凭代码瞎猜逻辑」的关键：
    必须让 LLM 看到客观错误（如 IndentationError、某用例期望 vs 实际）。
    """
    lines: list[str] = []
    run_results = values.get("last_run_results") or []
    if run_results:
        lines.append("【用户最近一次「运行」结果】")
        for r in run_results:
            d = r if isinstance(r, dict) else (r.model_dump() if hasattr(r, "model_dump") else {})
            st = d.get("status", "")
            tid = d.get("test_case_id", "")
            detail = (d.get("detail") or "").strip()
            inp = d.get("input_args", "")
            exp = d.get("expected", "")
            if st == "Passed":
                lines.append(f"- 用例#{tid} 通过")
            else:
                line = f"- 用例#{tid} 未通过（{st}）"
                if inp:
                    line += f"\n    输入：{inp}"
                if exp:
                    line += f"\n    期望输出：{exp}"
                if detail:
                    line += f"\n    错误信息：{detail}"
                lines.append(line)
    subs = values.get("submissions") or []
    if subs:
        last_sub = subs[-1]
        jrs = (
            last_sub.get("judge_results")
            if isinstance(last_sub, dict)
            else getattr(last_sub, "judge_results", [])
        ) or []
        if jrs:
            lines.append("【用户最近一次「提交判题」结果】")
            for jr in jrs:
                d = jr if isinstance(jr, dict) else (jr.model_dump() if hasattr(jr, "model_dump") else {})
                line = f"- {d.get('phase', '')} 阶段：{d.get('status', '')}"
                if d.get("detail"):
                    line += f" | {d.get('detail')}"
                if d.get("input_args"):
                    line += f"\n    输入：{d.get('input_args')}"
                if d.get("expected_output"):
                    line += f"\n    期望输出：{d.get('expected_output')}"
                if d.get("actual_output"):
                    line += f"\n    实际输出：{d.get('actual_output')}"
                lines.append(line)
    return "\n".join(lines) if lines else "（暂无运行/判题记录）"


# 历史轮次进入 LLM 上下文的最大消息条数（append-only 列表尾部追加；
# 只裁最旧消息并接受局部缓存失效——有前缀缓存后多带历史的边际成本极低，
# 阈值放宽反而能修掉「导师记得的对话 ≠ 用户看到的对话」的语义割裂）
_MAX_CHAT_CONTEXT_MESSAGES = 60


def _build_state_note(phase: str, verdict: str) -> str:
    """本轮辅导状态的动态说明（放当轮 human 消息开头，不污染静态 system 前缀）。"""
    if phase == "reviewing":
        # AC 后复盘/讨论模式
        return (
            "【本轮辅导状态】用户刚通过了一道算法题（AC），现在在回顾和讨论这道题。\n"
            "你的任务是：解释解题思路、时间/空间复杂度；讨论其他可能的解法"
            "（暴力/优化/不同数据结构）；分析这道题的易错点和面试常见追问；"
            "如果用户要求出下一题，引导他说出想练的方向。\n"
            "回复控制在 300 字以内。"
        )
    if verdict == "WA":
        # 刚提交 WA，正在辅导中
        return (
            "【本轮辅导状态】用户刚提交的代码没有通过（WA）。\n"
            "请基于对话上下文与下方客观运行/判题结果分析问题、给出针对性建议。\n"
            "不要直接给出完整代码，引导用户自己思考。"
        )
    # 用户正在做题的通用辅导
    return (
        "【本轮辅导状态】用户正在做题。\n"
        "不要直接给出完整代码，引导用户自己思考。"
    )


def _build_round_user_text(message: str, code: str, results: str) -> str:
    """单轮用户消息的规范文本：正文 + 当轮草稿代码 + 当轮运行/判题结果。

    写入 tutor_messages 时原样存进消息 metadata，下一轮请求按 metadata 原样重建，
    保证 append-only 前缀逐字节稳定（历史轮的旧草稿/旧结果只计费一次）。
    """
    return (
        f"用户消息：{message}\n\n"
        f"【用户当时的编辑器代码】\n"
        f"```python\n{code or '（编辑器为空）'}\n```\n\n"
        f"【用户当时的运行/判题结果】\n{results}"
    )


def _rebuild_history_messages(tutor_msgs, max_msgs: int = _MAX_CHAT_CONTEXT_MESSAGES) -> list:
    """把 tutor_messages 历史重建为 append-only 的 human/ai 消息序列。

    新格式消息（Message 对象带 metadata.round_text）原样重建；
    旧格式（dict / 无 metadata）退化为「用户消息：…」，仅一次性断缓存。
    """
    out: list = []
    for m in (tutor_msgs or [])[-max_msgs:]:
        if isinstance(m, dict):
            role = m.get("role", "tutor")
            content = m.get("content", "")
            round_text = (m.get("metadata") or {}).get("round_text")
        else:
            role = getattr(m, "role", "tutor")
            content = getattr(m, "content", "")
            round_text = (getattr(m, "metadata", None) or {}).get("round_text")
        if role == "user":
            out.append(HumanMessage(content=round_text or f"用户消息：{content}"))
        else:
            out.append(AIMessage(content=str(content)))
    return out


# 导师输出格式约束：避免把代码写成 ` ```python Solution: ` 这种畸形围栏、
# 或把整段代码/列表压缩成一行，导致前端 Markdown 无法正确渲染代码块。
_FORMAT_HINT = (
    "\n\n【输出格式要求】\n"
    "- 代码必须用标准围栏：单独一行写 ```python ，下一行起才是代码；"
    "严禁写成 ```python Solution: 这种把类名/说明塞进语言标识位的形式。\n"
    "- 代码块内每一行独立成行、保留真实缩进与换行，禁止把多行代码压缩成一行。\n"
    "- 讲解文字与代码块之间用空行分隔；列表项用 - 或 1. 2. 标准写法，每项单独一行。\n"
)

# 工具引导：导师可现场跑代码验证（详见设计文档 §2.3）
_JUDGE_HINT = (
    "\n\n你可以使用工具来**现场验证代码**（而不是凭空猜测结果）：\n"
    "- judge_run_code(source_code, stdin)：运行一段 Python 代码并返回 stdout/stderr/状态。"
    "当用户贴出代码问「这样写对不对 / 运行一下 / 帮我验证」时，优先用它真跑一遍。\n"
    "- judge_code(source_code, test_cases_json)：用一批用例判题（LeetCode 风格 class Solution）。\n"
    "- judge_check_health()：探测判题后端是否存活。\n"
    "只在用户确实贴了代码且需要验证时才调用，普通思路讨论不必调用。"
    "\n\n用户要代码 / 详细讲解时，请**直接在回复里写出**（你本身就能生成可运行代码与讲解），"
    "不要调用外部工具等待；清晰分节、用标准 ```python 围栏包裹代码即可。"
)

# 结果优先提示：约束导师严格依据客观运行/判题结果，避免臆造错误
_RESULT_HINT = (
    "\n\n【重要】下方「用户最近的运行与判题结果」是客观事实，请严格据此分析：\n"
    "- 若结果显示编译/语法/缩进错误（如 IndentationError、SyntaxError、CE），"
    "直接指出出错行与具体语法问题，并给出修正示例；不要笼统说「逻辑没闭环」。\n"
    "- 若某用例未通过，对照「期望输出 vs 实际输出」说明差异，再分析可能原因。\n"
    "- 只有当结果确实显示逻辑错误时，才去分析算法/逻辑问题。\n"
    "- 不要臆造用户没有遇到的错误；如果用户贴了代码请结合其真实运行结果回应。"
)

# 联网搜索工具引导：仅在配置了搜索 MCP 时注入（与 TUTOR_CHAT_TOOLS 是否含
# search 工具保持一致），避免向模型描述一个实际未绑定的工具。
_SEARCH_HINT = (
    "\n\n你可以使用 web_search(query, max_results) 工具联网搜索。使用规则：\n"
    "- 用户明确要求联网搜索 / 查最新信息（如「你搜一下」「联网查查」「最新版本是多少」）时，"
    "必须先调用 web_search，再据返回结果作答，禁止凭记忆直接回答。\n"
    "- 涉及时效性的事实（语言/库/框架的最新版本号、发布时间、近期资讯等）一律先搜再答，"
    "不要依赖你可能已过时的内置知识。\n"
    "- 搜索不可用或无结果时，如实说明未能联网检索，再基于已有知识作答。\n"
    "- 搜索与当前算法题无关的知识问题后，简短作答并自然地把话题带回题目。"
)

# 导师人设（全静态，收敛为单一变体：phase/verdict 变化不再制造新的 system 缓存线，
# 状态差异改为放当轮 human 消息开头，见 _build_state_note）
_TUTOR_SYSTEM = (
    "你是 AI 编程导师，语气温暖鼓励。用户正在做算法题，"
    "根据对话上下文分析问题、给出针对性建议。"
    "\n回复控制在 200 字以内。"
) + _JUDGE_HINT + (_SEARCH_HINT if search_mcp_configured() else "") + _RESULT_HINT + _FORMAT_HINT


async def _run_graph_and_generate_tests(graph, config, sid: str):
    """graph.invoke 出题后，再在后台生成完整测试套件（随机 + 边界）。

    与 session.py 的 fast-path / run_generation 对齐：agent 对话（贴 LeetCode
    链接或普通 topic/difficulty）出的题也要有完整用例，而不只是 1~2 个可见示例。
    LeetCode 路径 A 的 optimal_solution 已在 graph.invoke 内同步落库，可直接复用。

    注意：不直接复用 run_generation，因为它内部 SessionState(**initial_dict)
    会重建状态、清掉本路由已 update_state 的 leetcode / tutor_messages 数据。
    """
    cur = graph.get_state(config)
    await asyncio.to_thread(graph.invoke, dict(cur.values), config)
    from code_tutor_agent.api.services.generation import _run_suite_safe
    try:
        state = graph.get_state(config)
        problem = state.values.get("problem")
        if problem:
            pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
            reused = problem.reused if hasattr(problem, "reused") else (problem.get("reused") if isinstance(problem, dict) else False)
            if pid and not reused:
                logger.info("为 pid=%d 后台生成完整测试用例 (新题)", pid)
                await _run_suite_safe(pid, sid)
            else:
                logger.info("pid=%s 为复用题/无 pid，跳过测试生成", pid)
    except Exception as e:
        logger.error("Background complex test generation failed for %s: %s", sid, e, exc_info=True)


@router.post("/{sid}/chat/stream")
async def chat_with_tutor_stream(sid: str, body: dict, background_tasks: BackgroundTasks):
    """Streaming chat with the AI tutor via SSE."""
    graph = get_graph()
    _base = build_run_config(sid, run_name="chat_stream")
    try:
        state = graph.get_state(_base)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    # 记录活跃时间（TTL 清理用）
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    message = (body or {}).get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message is empty")
    code = (body or {}).get("code")

    values = state.values
    mode = values.get("mode", "")
    status = values.get("status", "")
    agent_done = values.get("agent_dialog_complete", False)

    # 富化 metadata（topic/difficulty/mode/problem_id）供 LangSmith 按会话筛查
    config = build_run_config(
        sid,
        mode=mode,
        topic=values.get("topic"),
        difficulty=values.get("difficulty"),
        problem_id=values.get("problem_id"),
        run_name="chat_stream",
    )

    # Agent 对话模式：仅当对话未完成时
    if status == "dialog" and mode == "agent" and not agent_done:
        return _handle_agent_dialog_stream(sid, config, graph, values, message, background_tasks)

    # 其余一律走常规辅导聊天（直接 LLM 流式 + 工具循环）
    return _handle_normal_chat_stream(sid, config, graph, values, message, code=code)


def _handle_agent_dialog_stream(sid, config, graph, values, message, background_tasks) -> StreamingResponse:
    """Agent 对话分支：意图分析 → 出题 or 继续追问，走 SSE 伪流式输出。

    注意：analyze_user_intent 必须在此函数体内按名导入（而非模块顶层），
    否则 test_agent_dialog_handoff 对 `agent_dialog.analyze_user_intent` 的 patch 会失效。
    """
    from code_tutor_agent.agents.agent_dialog import (
        DialogIntent,
        analyze_user_intent,
        build_ready_message,
    )

    raw_history = values.get("agent_dialog_history", [])
    # 统一转为 Message 对象（防御性：checkpointer 中可能混入 dict）
    history = _normalize_to_messages(raw_history)
    history.append(Message(role="user", content=message))

    def _fresh_display() -> list[Message]:
        """基于最新 state 的 tutor_messages 重建前端展示历史（竞态修复：
        不在请求开始时快照，避免流式期间并发写入被整表覆盖丢失）。"""
        fresh = _normalize_to_messages(graph.get_state(config).values.get("tutor_messages"))
        fresh.append(Message(role="user", content=message))
        return fresh

    graph.update_state(config, {
        "agent_dialog_history": history,
        "tutor_messages": _fresh_display(),
    }, as_node="agent_dialog_node")

    # 提取跨题摘要（Agent 模式换题时生成），注入到意图分析中
    context_summary = values.get("context_summary")
    if context_summary:
        logger.info("Agent dialog enriched with context_summary: %d chars", len(str(context_summary)))

    async def dialog_event_stream():
        nonlocal history, context_summary

        # ── 先判定意图（单次结构化调用），再决定回复内容与路由 ──
        # 把「自然回复」与「is_ready 路由判定」合并为同一次 LLM 判定，
        # 避免两个模型各说各话、互相矛盾（对话衔接修复-2）
        try:
            intent = await analyze_user_intent(history, context_summary=context_summary)
        except Exception as exc:
            logger.warning("analyze_user_intent failed: %s", exc)
            intent = DialogIntent(
                next_message="我没理解清楚，能再详细说说你想练什么类型的题吗？"
            )

        # ── 确定性兜底：修复 next-problem 重入 dialog 卡死 ──
        # analyze_user_intent 的 prompt 在「对话初期(1-2 轮)」分支与「随机/出题即 ready」
        # 分支存在矛盾，导致消息如"请随机给我出一道算法题"约 50% 概率被误判为
        # is_ready=False（被当成"初期需推荐方向"），使 next-problem 切换后无法触发出题、
        # 整个连续做题链路永久卡在 dialog 态。这里用显式关键词做确定性补强：
        # 命中"要求出题/交AI决定"信号且 LLM 未判 ready 时，强制推进到出题。
        if not intent.is_ready and (
            _explicit_generate_signals(message) or _explicit_random_signals(message)
        ):
            intent.is_ready = True
            # topic 与 difficulty 都缺失 → 用户把选择权交给 AI，走随机路径（保留惊喜感）
            if not intent.topic and not intent.difficulty:
                intent.is_random = True
            logger.info(
                "intent 兜底强制 is_ready=True（关键词命中，原 is_ready=False）"
            )

        if intent.is_ready:
            topic = intent.topic or values.get("topic", "数组")
            difficulty = intent.difficulty or values.get("difficulty", "easy")
            if intent.source == "leetcode" and intent.leetcode_url:
                # LeetCode 导入：用意图确认文案，避免误显「数组 方向、简单 难度」
                # （intent.topic/difficulty 对 leetcode 来源本就为空，硬填会误导）
                ready_msg = Message(role="tutor", content=intent.next_message)
            elif intent.is_random:
                # 随机出题：不向用户透露真实 topic/难度，保留惊喜感；
                # topic/difficulty 仍写入 state，供后端按实际选择出题
                ready_msg = Message(
                    role="tutor",
                    content="好的！我来为你准备一道 **随机方向**、**随机难度** 的题。请稍等，马上就好 🚀",
                )
            else:
                # 收尾回复固定为「正在生成题目」提示，不再让自由模型临场发挥
                # 说出「题目信息遗漏」这类错位文案（对话衔接修复-1）
                ready_msg = build_ready_message(topic, difficulty)
            history.append(ready_msg)
            # 立即置 awaiting_problem，前端可进入「生成中」视图（对话衔接修复-3）
            _display = _fresh_display()
            _display.append(ready_msg)
            _updates = {
                "agent_dialog_history": history,
                "agent_dialog_complete": True,
                "status": "awaiting_problem",
                "topic": topic,
                "difficulty": difficulty,
                "tutor_messages": _display,
            }
            # LeetCode 来源：仅把原始链接交给 generator_node，由其在服务端抓取并解析
            # （解析逻辑已收口到 generation 包，不再在路由层预解析）。
            if intent.source == "leetcode" and intent.leetcode_url:
                _updates["leetcode"] = {"url": intent.leetcode_url}
            graph.update_state(config, _updates, as_node="agent_dialog_node")

            # 伪流式输出固定收尾文案，保持打字效果
            for chunk in _chunk_text(ready_msg.content):
                yield _sse_payload(chunk)

            # 用 BackgroundTasks 可靠触发题目生成（planner→generator），
            # 并在 graph.invoke 后补跑完整测试套件（与 run_generation 对齐）
            background_tasks.add_task(_run_graph_and_generate_tests, graph, config, sid)
        else:
            # 非 ready：回复来自同一次判定的 next_message（已合并，无需再调自由模型）
            reply = intent.next_message or "好的，我明白了，能再具体说说吗？"
            history.append(Message(role="tutor", content=reply))
            _display = _fresh_display()
            _display.append(Message(role="tutor", content=reply))
            graph.update_state(config, {
                "agent_dialog_history": history,
                "tutor_messages": _display,
            }, as_node="agent_dialog_node")
            for chunk in _chunk_text(reply):
                yield _sse_payload(chunk)

        yield _sse_payload("__DONE__")

    return StreamingResponse(
        dialog_event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _handle_normal_chat_stream(sid, config, graph, values, message, code: str = "") -> StreamingResponse:
    """常规辅导聊天分支：直接 LLM 流式输出 + 工具循环（现场跑代码验证）。

    所有非 agent-dialog 聊天统一走直接 LLM 流式输出。
    原因：graph 可能在 interrupt 处（SqliteSaver 不支持 astream）
          或 graph 停在 END（如 AC 后 phase=reviewing），
          graph.stream(None, config) 从 END 不会重启，返回空。
    统一走直接 LLM 是最可靠的做法。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from code_tutor_agent.agents.tools import TUTOR_CHAT_TOOLS, run_tool_loop
    from code_tutor_agent.config import get_llm

    llm = get_llm(purpose="api-chat")

    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")
    desc = (
        problem.description
        if hasattr(problem, "description")
        else (problem.get("description", "") if problem else "")
    )

    _results_context = _build_results_context(values)

    # 根据 phase / last_verdict 生成当轮状态说明（放当轮 human 消息，不动静态 system）
    _phase = values.get("phase", "")
    _verdict = values.get("last_verdict", "")

    async def normal_chat_stream():
        # 用户当前编辑器代码（未提交）：只读注入 prompt，不写 state / 画像 / 轨迹。
        # 长度上限防止 token 浪费；导师应把它当「实时草稿」而非最终版本。
        _code_context = ""
        if code and code.strip():
            _code_limit = 4000
            if len(code) > _code_limit:
                _code_context = code[:_code_limit] + "\n# …（代码过长，已截断）"
            else:
                _code_context = code
        # 当轮消息 = 状态说明 + 规范轮次文本；完整文本随消息 metadata 持久化，
        # 下一轮按原样重建，保证 append-only 前缀稳定
        _round_text = _build_state_note(_phase, _verdict) + "\n\n" + _build_round_user_text(
            message, _code_context, _results_context
        )

        # ── append-only 消息列表（替代单条重建式 blob）──
        #   [system(全静态) + human(题面，本题内固定) + (ai 占位) + 历史轮次 + 当轮]
        # 前缀逐轮增长且逐字节不变 → 网关前缀缓存命中率随轮数趋近 100%。
        msgs = [
            SystemMessage(content=_TUTOR_SYSTEM),
            HumanMessage(content=f"算法题：{title}\n\n题面：\n{desc}"),
        ]
        _history = _rebuild_history_messages(values.get("tutor_messages"))
        if not _history or getattr(_history[0], "type", "") != "ai":
            # 题面之后需要一个 AI 占位消息，保证 human/ai 交替（前缀稳定）
            msgs.append(AIMessage(content="已了解题目，开始辅导。"))
        msgs.extend(_history)
        msgs.append(HumanMessage(content=_round_text))

        # ── 工具循环（非流式）：导师先决定是否跑代码验证 ──
        # return_last_content=True：若 LLM 没调工具（纯讨论），直接拿到首轮文本，
        # 下方复用它跳过 astream 二次调用，省一次 LLM 往返（方案 A）。
        _called_tool = False
        last_content = ""
        try:
            _loop_res = await run_tool_loop(llm, msgs, tools=TUTOR_CHAT_TOOLS, return_last_content=True)
            if isinstance(_loop_res, tuple):
                msgs, last_content = _loop_res
            else:
                msgs = _loop_res
            _called_tool = any(getattr(m, "type", None) == "tool" for m in msgs)
        except Exception as exc:
            logger.warning("Tutor tool loop failed (non-fatal): %s", exc)

        full = []
        if last_content and not _called_tool:
            # 纯讨论：复用首轮文本，按字符流式吐出（跳过 astream，省一次 LLM 调用）
            for _piece in _chunk_text(last_content):
                full.append(_piece)
                yield _sse_payload(_piece)
            reply = last_content
        else:
            # 调了工具 / 首轮为空：流式生成最终回复（msgs 已含工具结果）
            try:
                async for chunk in llm.astream(msgs):
                    token = chunk.content if hasattr(chunk, "content") else str(chunk)
                    if token:
                        full.append(token)
                        yield _sse_payload(token)
            except Exception as exc:
                logger.warning("Normal chat LLM failed: %s", exc)
            reply = "".join(full)
            if not reply.strip():
                # 验证工具失败 / 模型空回复：强制基于知识补答一次，
                # 避免把「沙箱挂了」误报成「无法回答」。
                logger.warning("Tutor final reply empty — retry with knowledge-only instruction")
                msgs.append(SystemMessage(content=(
                    "注意：代码验证工具未能返回有效结果（沙箱可能暂时不可用）。"
                    "请完全基于你的算法与数据结构知识直接回答用户的问题，"
                    "不要再调用任何 judge_* 工具；若用户问原理/推导，直接用文字和代码块讲解。"
                )))
                full = []
                try:
                    async for chunk in llm.astream(msgs):
                        token = chunk.content if hasattr(chunk, "content") else str(chunk)
                        if token:
                            full.append(token)
                            yield _sse_payload(token)
                except Exception as exc:
                    logger.warning("Normal chat LLM retry failed: %s", exc)
                reply = "".join(full)
            if not reply.strip():
                reply = "代码验证服务暂时不可用，但我可以基于算法原理直接为你解答，请稍后重试或换个问法。"
            if not full:
                yield _sse_payload(reply)

        # 手动保存到 state（暂停安全写入：直接 update_state 会丢失
        # wait_for_submit 的挂起中断，见 deps.pause_safe_update）
        # 竞态修复：请求开始时的 values 快照在流式生成期间可能已过期
        # （并发 /submit 判题会追加 tutor 消息），必须先基于最新 state 追加，
        # 否则旧快照整表覆盖会丢并发写入（last-wins lost update）。
        # 当轮用户消息携带完整 round_text，供下一轮按原样重建（缓存前缀稳定）。
        try:
            fresh_msgs = graph.get_state(config).values.get("tutor_messages") or []
            tutor_msgs = list(fresh_msgs)
            tutor_msgs.append(Message(role="user", content=message, metadata={"round_text": _round_text}))
            tutor_msgs.append(Message(role="tutor", content=reply))
        except Exception as exc:
            logger.warning("Failed to read fresh state for chat persist: %s", exc)
            tutor_msgs = list(values.get("tutor_messages", []))
            tutor_msgs.append({"role": "user", "content": message})
            tutor_msgs.append({"role": "tutor", "content": reply})
        try:
            from code_tutor_agent.api.deps import pause_safe_update
            pause_safe_update(graph, config, {"tutor_messages": tutor_msgs})
        except Exception as exc:
            logger.exception("Failed to save chat: %s", exc)
        yield _sse_payload("__DONE__")

    return StreamingResponse(normal_chat_stream(), media_type="text/event-stream")
