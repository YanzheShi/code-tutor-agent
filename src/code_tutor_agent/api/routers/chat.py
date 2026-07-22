"""Chat router — streaming + non-streaming chat endpoints."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from starlette.responses import StreamingResponse

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)
router = APIRouter()


def _chunk_text(text: str, size: int = 6):
    """把文本切成小段，用于伪流式输出，保持打字效果。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


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


# 导师输出格式约束：避免把代码写成 ` ```python Solution: ` 这种畸形围栏、
# 或把整段代码/列表压缩成一行，导致前端 Markdown 无法正确渲染代码块。
_FORMAT_HINT = (
    "\n\n【输出格式要求】\n"
    "- 代码必须用标准围栏：单独一行写 ```python ，下一行起才是代码；"
    "严禁写成 ```python Solution: 这种把类名/说明塞进语言标识位的形式。\n"
    "- 代码块内每一行独立成行、保留真实缩进与换行，禁止把多行代码压缩成一行。\n"
    "- 讲解文字与代码块之间用空行分隔；列表项用 - 或 1. 2. 标准写法，每项单独一行。\n"
)


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
    from code_tutor_agent.api.services.generation import _generate_complex_tests
    try:
        state = graph.get_state(config)
        problem = state.values.get("problem")
        if problem:
            pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
            if pid:
                await asyncio.to_thread(_generate_complex_tests, pid, sid)
    except Exception as e:
        logger.error("Background complex test generation failed for %s: %s", sid, e, exc_info=True)


@router.post("/{sid}/chat/stream")
async def chat_with_tutor_stream(sid: str, body: dict, background_tasks: BackgroundTasks):
    """Streaming chat with the AI tutor via SSE."""
    from code_tutor_agent.config import get_llm
    from code_tutor_agent.agents.agent_dialog import (
        analyze_user_intent,
        build_ready_message,
        DialogIntent,
    )

    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}
    try:
        state = graph.get_state(config)
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

    values = state.values
    status = values.get("status", "")
    mode = values.get("mode", "")
    agent_done = values.get("agent_dialog_complete", False)

    # Agent 对话模式：仅当对话未完成时
    if status == "dialog" and mode == "agent" and not agent_done:
        raw_history = values.get("agent_dialog_history", [])
        # 统一转为 Message 对象（防御性：checkpointer 中可能混入 dict）
        history: list[Message] = []
        for m in raw_history:
            if isinstance(m, Message):
                history.append(m)
            elif isinstance(m, dict):
                history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                history.append(Message(role="tutor", content=str(m)))
        history.append(Message(role="user", content=message))

        # 构建完整前端展示历史（保留跨题对话，不清空）
        _full_display: list[Message] = []
        for m in (values.get("tutor_messages") or []):
            if isinstance(m, Message):
                _full_display.append(m)
            elif isinstance(m, dict):
                _full_display.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _full_display.append(Message(role="tutor", content=str(m)))
        _full_display.append(Message(role="user", content=message))

        graph.update_state(config, {
            "agent_dialog_history": history,
            "tutor_messages": _full_display,
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

            if intent.is_ready:
                topic = intent.topic or values.get("topic", "数组")
                difficulty = intent.difficulty or values.get("difficulty", "easy")
                # LeetCode 来源：用解析数据里的标题/难度，给更贴合的收尾文案。
                # 防御：LLM 可能臆造 source=leetcode 并只给一个题号（int），
                # 此时 leetcode_payload 解析后不是含 title 的 dict，应回退为生成式出题。
                leetcode_data = None
                if intent.source == "leetcode" and intent.leetcode_payload:
                    try:
                        _parsed = json.loads(intent.leetcode_payload)
                        if isinstance(_parsed, dict) and _parsed.get("title"):
                            leetcode_data = _parsed
                            topic = _parsed.get("title") or topic
                            difficulty = _parsed.get("difficulty") or difficulty
                    except json.JSONDecodeError:
                        pass
                    if leetcode_data is None:
                        intent = intent.model_copy(update={"source": "generated"})
                # 收尾回复固定为「正在生成题目」提示，不再让自由模型临场发挥
                # 说出「题目信息遗漏」这类错位文案（对话衔接修复-1）
                ready_msg = build_ready_message(topic, difficulty)
                history.append(ready_msg)
                _full_display.append(ready_msg)
                # 立即置 awaiting_problem，前端可进入「生成中」视图（对话衔接修复-3）
                _updates = {
                    "agent_dialog_history": history,
                    "agent_dialog_complete": True,
                    "status": "awaiting_problem",
                    "topic": topic,
                    "difficulty": difficulty,
                    "tutor_messages": _full_display,
                }
                if intent.source == "leetcode" and leetcode_data:
                    _updates["leetcode"] = leetcode_data
                graph.update_state(config, _updates, as_node="agent_dialog_node")

                # 伪流式输出固定收尾文案，保持打字效果
                for chunk in _chunk_text(ready_msg.content):
                    yield f"data: {chunk}\n\n"

                # 用 BackgroundTasks 可靠触发题目生成（planner→generator），
                # 并在 graph.invoke 后补跑完整测试套件（与 run_generation 对齐）
                background_tasks.add_task(_run_graph_and_generate_tests, graph, config, sid)
            else:
                # 非 ready：回复来自同一次判定的 next_message（已合并，无需再调自由模型）
                reply = intent.next_message or "好的，我明白了，能再具体说说吗？"
                history.append(Message(role="tutor", content=reply))
                _full_display.append(Message(role="tutor", content=reply))
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "tutor_messages": _full_display,
                }, as_node="agent_dialog_node")
                for chunk in _chunk_text(reply):
                    yield f"data: {chunk}\n\n"

            yield "data: __DONE__\n\n"

        return StreamingResponse(
            dialog_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Normal tutoring chat ──
    # 所有非 agent-dialog 聊天统一走直接 LLM 流式输出。
    # 原因：graph 可能在 interrupt 处（SqliteSaver 不支持 astream）
    #       或 graph 停在 END（如 AC 后 phase=reviewing），
    #       graph.stream(None, config) 从 END 不会重启，返回空。
    # 统一走直接 LLM 是最可靠的做法。
    from code_tutor_agent.config import get_llm
    llm = get_llm("agnes-stream", temperature=0.7)

    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")
    pid = problem.problem_id if hasattr(problem, "problem_id") else (problem.get("problem_id") if problem else None)
    desc = (
        problem.description
        if hasattr(problem, "description")
        else (problem.get("description", "") if problem else "")
    )

    # 当前题上下文写入 contextvar，供工具里 *_via_skill 日志回溯
    from code_tutor_agent.agents.tools import current_problem_ctx
    current_problem_ctx.set({"problem_id": pid, "title": title})

    # 收集 tutor_messages 中近期对话作为上下文
    _tutor_msgs = values.get("tutor_messages") or []
    _context_lines = []
    for msg in _tutor_msgs[-8:]:  # 只取最近 8 条
        role = msg.get("role", "tutor") if isinstance(msg, dict) else getattr(msg, "role", "tutor")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if content:
            label = "导师" if role == "tutor" else "用户"
            _context_lines.append(f"{label}: {content}")
    _chat_context = "\n".join(_context_lines) if _context_lines else "（无）"

    _results_context = _build_results_context(values)

    # 根据 phase 匹配合适的 system prompt
    _phase = values.get("phase", "")
    _verdict = values.get("last_verdict", "")

    if _phase == "reviewing":
        # AC 后复盘/讨论模式
        system = (
            "你是 AI 编程导师，用户刚通过了一道算法题（AC），"
            "现在在回顾和讨论这道题。你的任务是：\n"
            "- 解释解题思路、时间/空间复杂度\n"
            "- 讨论其他可能的解法（暴力/优化/不同数据结构）\n"
            "- 分析这道题的易错点和面试常见追问\n"
            "- 如果用户要求出下一题，引导他说出想练的方向\n"
            "语气鼓励、专业，回复控制在 300 字以内。"
        )
    elif _verdict == "WA":
        # 刚提交 WA，正在辅导中
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户刚提交的代码没有通过（WA），"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )
    else:
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户正在做题，"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )

    # 工具引导：导师可现场跑代码验证（详见设计文档 §2.3）
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, TUTOR_CHAT_TOOLS

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

    async def normal_chat_stream():
        user_prompt = (
            f"算法题：{title}\n\n"
            f"题面：\n{desc}\n\n"
            f"用户最近的运行与判题结果（客观事实，请优先依据此定位具体错误）：\n"
            f"{_results_context}\n\n"
            f"近期对话：\n{_chat_context}\n\n"
            f"用户当前消息：{message}"
        )
        # 用 LangChain Message 列表承载上下文（工具循环需就地追加 ToolMessage）
        _RESULT_HINT = (
            "\n\n【重要】下方「用户最近的运行与判题结果」是客观事实，请严格据此分析：\n"
            "- 若结果显示编译/语法/缩进错误（如 IndentationError、SyntaxError、CE），"
            "直接指出出错行与具体语法问题，并给出修正示例；不要笼统说「逻辑没闭环」。\n"
            "- 若某用例未通过，对照「期望输出 vs 实际输出」说明差异，再分析可能原因。\n"
            "- 只有当结果确实显示逻辑错误时，才去分析算法/逻辑问题。\n"
            "- 不要臆造用户没有遇到的错误；如果用户贴了代码请结合其真实运行结果回应。"
        )
        msgs = [
            SystemMessage(content=system + _JUDGE_HINT + _RESULT_HINT + _FORMAT_HINT),
            HumanMessage(content=user_prompt),
        ]
        # ── 工具循环（非流式）：导师先决定是否跑代码验证 ──
        try:
            await run_tool_loop(llm, msgs, tools=TUTOR_CHAT_TOOLS)
        except Exception as exc:
            logger.warning("Tutor tool loop failed (non-fatal): %s", exc)
        # ── 流式输出最终回复（msgs 已含工具结果）──
        full = []
        try:
            async for chunk in llm.astream(msgs):
                token = chunk.content if hasattr(chunk, "content") else str(chunk)
                if token:
                    full.append(token)
                    yield f"data: {token}\n\n"
        except Exception as exc:
            logger.warning("Normal chat LLM failed: %s", exc)

        reply = "".join(full)
        if not reply.strip():
            reply = "抱歉，我现在无法回答。请稍后再试。"
        if not full:
            yield f"data: {reply}\n\n"

        # 手动保存到 state
        tutor_msgs = list(values.get("tutor_messages", []))
        tutor_msgs.append({"role": "user", "content": message})
        tutor_msgs.append({"role": "tutor", "content": reply})
        try:
            graph.update_state(config, {"tutor_messages": tutor_msgs})
        except Exception as exc:
            logger.warning("Failed to save chat: %s", exc)
        yield "data: __DONE__\n\n"

    return StreamingResponse(normal_chat_stream(), media_type="text/event-stream")


@router.post("/{sid}/chat")
async def chat_with_tutor(sid: str, body: dict, background_tasks: BackgroundTasks):
    """Non-streaming chat with the AI tutor."""
    from code_tutor_agent.config import get_llm

    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}
    try:
        state = graph.get_state(config)
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

    values = state.values
    status = values.get("status", "")
    mode = values.get("mode", "")

    # ── Agent dialog mode (non-streaming fallback) ──
    if status == "dialog" and mode == "agent" and not values.get("agent_dialog_complete", False):
        from code_tutor_agent.agents.agent_dialog import analyze_user_intent, build_ready_message

        raw_history = values.get("agent_dialog_history", [])
        # 统一转为 Message 对象（防御性：checkpointer 中可能混入 dict）
        history: list[Message] = []
        for m in raw_history:
            if isinstance(m, Message):
                history.append(m)
            elif isinstance(m, dict):
                history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                history.append(Message(role="tutor", content=str(m)))
        history.append(Message(role="user", content=message))

        # 构建完整前端展示历史（保留跨题对话，不清空）
        _full_display: list[Message] = []
        for m in (values.get("tutor_messages") or []):
            if isinstance(m, Message):
                _full_display.append(m)
            elif isinstance(m, dict):
                _full_display.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _full_display.append(Message(role="tutor", content=str(m)))
        _full_display.append(Message(role="user", content=message))

        graph.update_state(config, {
            "agent_dialog_history": history,
            "tutor_messages": _full_display,
        }, as_node="agent_dialog_node")

        context_summary = values.get("context_summary")
        intent = await analyze_user_intent(history, context_summary=context_summary)

        if intent.is_ready:
            topic = intent.topic or values.get("topic", "数组")
            difficulty = intent.difficulty or values.get("difficulty", "easy")
            # LeetCode 来源：用解析数据里的标题/难度，给更贴合的收尾文案
            if intent.source == "leetcode" and intent.leetcode_payload:
                try:
                    _lc = json.loads(intent.leetcode_payload)
                    topic = _lc.get("title") or topic
                    difficulty = _lc.get("difficulty") or difficulty
                except json.JSONDecodeError:
                    pass
            ready_msg = build_ready_message(topic, difficulty)
            history.append(ready_msg)
            _full_display.append(ready_msg)
            _updates = {
                "agent_dialog_history": history,
                "agent_dialog_complete": True,
                "topic": topic,
                "difficulty": difficulty,
                "tutor_messages": _full_display,
            }
            if intent.source == "leetcode" and intent.leetcode_payload:
                try:
                    _updates["leetcode"] = json.loads(intent.leetcode_payload)
                except json.JSONDecodeError:
                    pass
            graph.update_state(config, _updates, as_node="agent_dialog_node")
            # 后台 graph.invoke 出题 + 生成完整测试套件（与 run_generation 对齐）
            background_tasks.add_task(_run_graph_and_generate_tests, graph, config, sid)
            return {"response": intent.next_message or ready_msg.content}
        else:
            history.append(Message(role="tutor", content=intent.next_message))
            _full_display.append(Message(role="tutor", content=intent.next_message))
            graph.update_state(config, {
                "agent_dialog_history": history,
                "tutor_messages": _full_display,
            }, as_node="agent_dialog_node")
            return {"response": intent.next_message}

    # ── Normal tutoring chat (non-streaming) ──
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")
    pid = problem.problem_id if hasattr(problem, "problem_id") else (problem.get("problem_id") if problem else None)
    desc = (
        problem.description
        if hasattr(problem, "description")
        else (problem.get("description", "") if problem else "")
    )

    # 当前题上下文写入 contextvar，供工具里 *_via_skill 日志回溯
    from code_tutor_agent.agents.tools import current_problem_ctx
    current_problem_ctx.set({"problem_id": pid, "title": title})

    # ── Normal tutoring chat (non-streaming): 统一走直接 LLM ──
    # 原因同流式路径：graph 可能在 interrupt 或 END，无法可靠地通过 graph.invoke 走 chat_node
    llm = get_llm("agnes", temperature=0.7)

    _phase = values.get("phase", "")
    _verdict = values.get("last_verdict", "")

    if _phase == "reviewing":
        system = (
            "你是 AI 编程导师，用户刚通过了一道算法题（AC），"
            "现在在回顾和讨论这道题。你的任务是：\n"
            "- 解释解题思路、时间/空间复杂度\n"
            "- 讨论其他可能的解法\n"
            "- 分析易错点和面试常见追问\n"
            "语气鼓励、专业，回复控制在 300 字以内。"
        )
    elif _verdict == "WA":
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户刚提交的代码没有通过（WA），"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )
    else:
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户正在做题，"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )

    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, TUTOR_CHAT_TOOLS

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

    _results_context = _build_results_context(values)
    user_prompt = (
        f"算法题：{title}\n\n"
        f"题面：\n{desc}\n\n"
        f"用户最近的运行与判题结果（客观事实，请优先依据此定位具体错误）：\n"
        f"{_results_context}\n\n"
        f"用户当前消息：{message}"
    )
    _RESULT_HINT = (
        "\n\n【重要】下方「用户最近的运行与判题结果」是客观事实，请严格据此分析：\n"
        "- 若结果显示编译/语法/缩进错误（如 IndentationError、SyntaxError、CE），"
        "直接指出出错行与具体语法问题，并给出修正示例；不要笼统说「逻辑没闭环」。\n"
        "- 若某用例未通过，对照「期望输出 vs 实际输出」说明差异，再分析可能原因。\n"
        "- 只有当结果确实显示逻辑错误时，才去分析算法/逻辑问题。\n"
        "- 不要臆造用户没有遇到的错误；如果用户贴了代码请结合其真实运行结果回应。"
    )
    msgs = [
        SystemMessage(content=system + _JUDGE_HINT + _RESULT_HINT + _FORMAT_HINT),
        HumanMessage(content=user_prompt),
    ]
    try:
        # 工具循环（非流式）：导师先决定是否跑代码验证，再生成最终回复
        await run_tool_loop(llm, msgs, tools=TUTOR_CHAT_TOOLS)
        response = llm.invoke(msgs)  # 最终回复（未绑工具，纯文本）
        reply = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.warning("Chat LLM failed: %s", exc)
        reply = "抱歉，我现在无法回答。请稍后再试。"

    current_msgs = list(values.get("tutor_messages", []))
    current_msgs.append({"role": "user", "content": message})
    current_msgs.append({"role": "tutor", "content": reply})
    graph.update_state(config, {"tutor_messages": current_msgs})

    return {"response": reply}