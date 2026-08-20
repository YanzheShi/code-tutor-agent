"""轨迹分析独立线程（按题隔离、多轮追问、持久化；绝不写 profile / memory）。

线程消息持久化在 trace_threads 表（key = analyze:<sid>:<pid> 语义的独立隔离单元），
进程内 dict 仅作缓存；服务重启后多轮追问与过渡压缩仍可读回 transcript。
结构化输出走默认 json_schema（thinking 模式安全，严禁 function_calling）。

非致命：任何异常都返回带说明的结果，不影响主流程。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from code_tutor_agent.config import get_llm, get_trace_retry_config
from code_tutor_agent.context_manager import count_tokens
from code_tutor_agent.db.database import (
    delete_trace_thread,
    get_edit_trace_by_problem,
    get_problem_by_id,
    get_submissions_by_session,
    get_trace_thread,
    save_analysis_result,
    save_trace_thread,
)
from code_tutor_agent.trace.preprocess import build_dialogue_timeline, pick_code_snapshots
from code_tutor_agent.trace.extract import extract_for_analysis
from code_tutor_agent.trace.schemas import AnalysisResult

logger = logging.getLogger(__name__)

# 进程内线程缓存：key = f"analyze:{session_id}:{problem_id}" → list[BaseMessage]
# 所有变更写穿到 trace_threads 表（持久化），缓存只为避免重复反序列化。
_THREADS: dict[str, list] = {}

_SYSTEM = (
    "你是一个编程教练，专门分析一次算法题「做题过程」的编辑轨迹，帮助用户复盘。"
    "注意：本次分析是**独立**的、仅供用户自我复盘参考，**不要**写成能力评分，也**不要**关联任何历史画像。"
    "你已知晓每次代码改动前导师说了什么（时间线里的「导师说：...」），据此判断用户是「独立改对」还是「被提示改对」。"
)

# 首轮任务指令（全静态，随 _SYSTEM 一起升格为 system message：
# 跨题/跨会话/跨用户共享同一条缓存前缀；同时修复「不要关联历史画像」约束首轮不生效的问题）
_SYSTEM_TASK = (
    "## 任务\n基于轨迹与最终代码产出结构化复盘：\n"
    "1. change_path：用若干步骤叙述代码怎么一步步写出来"
    "（每步标 trigger：self/hint/boundary_reminder/correction_assisted）。\n"
    "2. thinking_process：推断解题思维过程（是否走弯路、是否先写暴力再优化、是否提交前自查）。\n"
    "3. weakness_tags：暴露的薄弱点，每条给 evidence、severity(0~1)、trigger、"
    "hint_assisted、hints_before_fix。\n"
    "4. interview_tips：2-4 条备考建议（point + reason）。\n"
    "5. autonomy：self_fix_rate(0~1) 与 hint_dependent_weaknesses 列表（面试就绪度信号）。\n"
    "6. summary：一句话总评。\n"
    "只输出结构化结果。"
)

# 首轮分析动态数据块的总 token 预算（对比导师链路 ContextConfig；超预算时对
# 时间线减半裁剪，而不是各截断参数各自为政地堆叠）
_FIRST_ROUND_TOKEN_BUDGET = 24_000


def _thread_key(session_id: str, problem_id: str) -> str:
    return f"analyze:{session_id}:{problem_id}"


# ── LLM 调用重试（429 等瞬时错误指数退避）──


def _is_transient_error(exc: BaseException) -> bool:
    """429 / 限流 / 超时 / 网络类错误视为可重试的瞬时错误。"""
    msg = str(exc).lower()
    if isinstance(exc, ConnectionError):
        return True
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return True
    return False


def _invoke_with_retry(fn, attempts: Optional[int] = None, base_delay: Optional[float] = None):
    """带指数退避的 LLM 调用封装：瞬时错误（429/限流/超时/网络）重试。

    参数缺省时从环境变量读取（TRACE_RETRY_ATTEMPTS / TRACE_RETRY_BASE_DELAY_SECONDS，
    默认 4 次、30s 起步），RPM 限流时无需改代码即可调大间隔。
    非瞬时异常直接抛出；重试耗尽后抛最后一次异常，由调用方兜底降级。
    """
    if attempts is None or base_delay is None:
        _retry_cfg = get_trace_retry_config()
        attempts = _retry_cfg["attempts"] if attempts is None else attempts
        base_delay = _retry_cfg["base_delay"] if base_delay is None else base_delay
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not _is_transient_error(exc):
                break
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "trace LLM 调用失败（attempt %d/%d）: %s — %.1fs 后重试",
                attempt + 1, attempts, exc, delay,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── 线程消息序列化（BaseMessage ⇄ dict）──

_MSG_CLS = {"system": SystemMessage, "human": HumanMessage, "ai": AIMessage}


def _serialize_messages(messages: list[BaseMessage]) -> list[dict]:
    out = []
    for m in messages:
        content = m.content
        if isinstance(content, list):
            content = "".join(str(p) for p in content)
        out.append({"type": m.type, "content": str(content)})
    return out


def _deserialize_messages(rows: list[dict]) -> list[BaseMessage]:
    msgs = []
    for r in rows:
        cls = _MSG_CLS.get(r.get("type"), HumanMessage)
        msgs.append(cls(content=r.get("content", "")))
    return msgs


def _load_thread(session_id: str, problem_id: str) -> Optional[list]:
    """读线程（缓存优先，miss 则回源 trace_threads 表）。"""
    key = _thread_key(session_id, problem_id)
    if key in _THREADS:
        return _THREADS[key]
    rows = get_trace_thread(session_id, problem_id)
    if not rows:
        return None
    msgs = _deserialize_messages(rows)
    _THREADS[key] = msgs
    return msgs


def _save_thread(session_id: str, problem_id: str, messages: list) -> None:
    """写线程（缓存 + 落库，写库失败仅告警不阻断）。"""
    key = _thread_key(session_id, problem_id)
    _THREADS[key] = messages
    try:
        save_trace_thread(session_id, problem_id, _serialize_messages(messages))
    except Exception as exc:
        logger.warning("save_trace_thread failed: %s", exc)


def load_thread_messages(session_id: str, problem_id: str) -> list:
    """供过渡压缩读取线程 transcript（无则空列表）。"""
    return _load_thread(session_id, problem_id) or []


def list_thread_for_display(session_id: str, problem_id: str) -> list[dict]:
    """导出线程中"可展示"的消息（跳过 system 与首轮结论 AI 消息）。

    首轮结论已由 analysis_results 表承载（GET /analysis 的 analysis 字段），
    这里只返回多轮追问的 {role: user|tutor, content}。
    """
    msgs = _load_thread(session_id, problem_id) or []
    out: list[dict] = []
    seen_analysis = False
    for m in msgs:
        if m.type == "system":
            continue
        if m.type == "ai" and not seen_analysis:
            seen_analysis = True
            continue
        content = m.content
        if isinstance(content, list):
            content = "".join(str(p) for p in content)
        out.append({"role": "tutor" if m.type == "ai" else "user", "content": str(content)})
    return out


# ── 题目上下文反查 ──


def _gather_context(
    session_id: str,
    problem_id: str,
    problem_meta=None,
) -> tuple[str, str, str, str, str]:
    """反查本题 final_code / topic / description / constraints / examples。

    优先用会话状态里的 ProblemMeta（含完整描述 + 约束 + 示例）；
    终码来自同题 AC 提交（无 AC 取最新提交）；题目库 DBProblem 作兜底。
    """
    final_code = ""
    topic = ""
    description = ""
    constraints = ""
    examples = ""
    pid_int = None
    try:
        if problem_id not in (None, "default"):
            try:
                pid_int = int(problem_id)
            except (ValueError, TypeError):
                pid_int = None

        subs = get_submissions_by_session(session_id) or []
        # 只取本题的提交（会话可跨多题），且按 id DESC 新在前 → 无 AC 时取最新
        if pid_int is not None:
            subs = [
                s for s in subs
                if s.get("problem_id") is not None and int(s["problem_id"]) == pid_int
            ]
        if subs:
            ac = next((s for s in subs if s.get("verdict") == "AC"), subs[0])
            final_code = ac.get("code") or ""
            try:
                pid_int = int(ac["problem_id"])
            except (KeyError, TypeError, ValueError):
                pass

        prob = None
        if pid_int is not None:
            prob = get_problem_by_id(pid_int)

        if problem_meta is not None:
            topic = problem_meta.topic or ""
            description = problem_meta.description or ""
            constraints = "\n".join(problem_meta.constraints or [])
            examples = "\n".join(problem_meta.examples or [])

        if prob is not None:
            topic = topic or (prob.topic or "")
            description = description or (prob.description or "")
            if not constraints:
                try:
                    cj = json.loads(prob.constraints_json or "[]")
                    constraints = "\n".join(cj) if isinstance(cj, list) else str(cj)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("trace _gather_context failed: %s", exc)
    return final_code, topic, description, constraints, examples


def _format_snapshots(snaps: list[dict]) -> str:
    parts = []
    for i, s in enumerate(snaps, 1):
        parts.append(
            f"### 快照 {i}（{s.get('event')}，ts={s.get('ts')}，改动: {s.get('change') or '—'}）\n"
            f"```python\n{s.get('code') or ''}\n```"
        )
    return "\n\n".join(parts)


def _format_stuck_segments(segments: list[dict]) -> str:
    """把结构化卡壳段拼成可读文本，喂给分析 LLM（让其精准归因"卡在哪/为何卡"）。"""
    if not segments:
        return "(无显著卡壳停顿)"
    parts = []
    for i, s in enumerate(segments, 1):
        idle_s = (s.get("idleMs") or 0) / 1000.0
        level = s.get("level") or "?"
        code = s.get("code_at_pause") or s.get("pre_code") or ""
        dlg = s.get("dialogue_before")
        dlg_text = " 导师说: " + " | ".join(
            d.get("content", "") for d in (dlg or []) if isinstance(d, dict) and d.get("content")
        ) if dlg else ""
        away = " (疑似离开/查资料，非真卡壳)" if s.get("away") else ""
        parts.append(
            f"### 卡壳 {i}（停顿 {idle_s:.1f}s，level={level}）{away}\n"
            f"卡壳时代码状态:\n```python\n{code}\n```"
            + (f"\n卡壳前刚收到的导师提示:{dlg_text}" if dlg_text else "")
        )
    return "\n\n".join(parts)


# ── 对外入口 ──


def first_round_analysis(session_id: str, problem_id: str, problem_meta=None) -> AnalysisResult:
    """首轮结构化分析：读按题过滤的轨迹 + 题目完整描述 + 终码 → LLM 结构化复盘。

    线程 = [system, 首轮结论(AI)]，落 trace_threads 表；多轮追问在其后 append。

    缓存结构：_SYSTEM + 任务指令升格为静态 system message（跨题/跨会话/跨用户
    共享同一前缀）；动态数据（题面/终码/快照/时间线）全在 human 消息里，
    且带总 token 预算（超预算对时间线减半裁剪）。
    """
    events = get_edit_trace_by_problem(session_id, problem_id)
    if not events:
        return AnalysisResult(summary="这道题没有采集到编辑轨迹，无法生成复盘。")
    final_code, topic, description, constraints, examples = _gather_context(
        session_id, problem_id, problem_meta
    )

    # ★ 改为调用抽取层（零 LLM）：把密集全量事件蒸馏成时间线 + 里程碑 + 结构化卡壳段
    extraction = extract_for_analysis(events)

    data = (
        f"## 本题信息\n- 知识点: {topic or '(未知)'}\n"
        f"- 题目描述:\n{(description or '(未知)')[:8000]}\n"
        f"- 约束条件:\n{constraints or '无'}\n"
        f"- 示例:\n{examples or '无'}\n\n"
        f"## 最终提交的代码\n```python\n{(final_code or '')[:4000]}\n```\n\n"
        f"## 关键代码快照（按时间先后）\n{_format_snapshots(extraction.snapshots) or '(无)'}\n\n"
        f"## 卡壳段（结构化，stuck_segments）\n{_format_stuck_segments(extraction.stuck_segments)}\n\n"
        f"## 编辑轨迹时间线（含相邻快照 diff 与改动前的导师提示原文）\n{extraction.timeline_text}"
    )

    # 抽取层内已做 token 预算减半裁剪；此处仅打点观测是否触发
    tokens = count_tokens(data)
    if tokens > _FIRST_ROUND_TOKEN_BUDGET:
        logger.info(
            "first_round_analysis: extracted data %d tokens > budget %d (timeline 已减半裁剪)",
            tokens, _FIRST_ROUND_TOKEN_BUDGET,
        )

    msgs = [
        SystemMessage(content=_SYSTEM + "\n\n" + _SYSTEM_TASK),
        HumanMessage(content=data),
    ]

    try:
        llm = get_llm(purpose="edit-trace")
        # 默认 method（json_schema）：thinking 模式下 function_calling 的强制 tool_choice 会 400，
        # 默认 json_schema 路径可用（与 judge/problem 等一致）。
        structured = llm.with_structured_output(AnalysisResult)
        result = _invoke_with_retry(lambda: structured.invoke(msgs))
        if not isinstance(result, AnalysisResult):
            result = AnalysisResult(**(result if isinstance(result, dict) else {}))
        # 线程：system + 首轮结构化结论（AI 消息），供多轮追问与过渡压缩参考
        _save_thread(session_id, problem_id, [
            SystemMessage(content=_SYSTEM),
            AIMessage(content=result.model_dump_json()),
        ])
        try:
            save_analysis_result(
                session_id, problem_id, result.model_dump(),
                model=getattr(llm, "model_name", "") or "",
            )
        except Exception as exc:
            logger.warning("save_analysis_result failed: %s", exc)
        return result
    except Exception as exc:
        logger.error("first_round_analysis LLM failed for %s/%s: %s", session_id, problem_id, exc)
        return AnalysisResult(summary="轨迹分析暂时不可用（模型调用失败），稍后再试一次吧。")


def continue_analysis(session_id: str, problem_id: str, message: str) -> str:
    """多轮追问：在同题分析线程追加用户问题，返回自由文本回复。"""
    msgs = _load_thread(session_id, problem_id)
    if not msgs:
        # 线程不存在（如刷新后或已被归档）：先跑首轮建立上下文，再回答追问
        first_round_analysis(session_id, problem_id)
        msgs = _load_thread(session_id, problem_id) or []
    msgs.append(HumanMessage(content=message))
    # 先落库（含未回答的问题），保证缓存与 DB 一致；失败也只在内存里推进
    _save_thread(session_id, problem_id, msgs)
    try:
        llm = get_llm(purpose="edit-trace")
        reply = _invoke_with_retry(lambda: llm.invoke(msgs))
        text = reply.content if hasattr(reply, "content") else str(reply)
        if isinstance(text, list):
            text = "".join(str(part) for part in text)
        msgs.append(AIMessage(content=text))
        _save_thread(session_id, problem_id, msgs)
        return text
    except Exception as exc:
        logger.error("continue_analysis LLM failed: %s", exc)
        return "（分析追问暂时不可用，请稍后再试。）"


def archive_thread(session_id: str, problem_id: str) -> None:
    """过渡时归档（清空）当前题分析线程：缓存 + trace_threads 表。"""
    _THREADS.pop(_thread_key(session_id, problem_id), None)
    try:
        delete_trace_thread(session_id, problem_id)
    except Exception as exc:
        logger.warning("archive_thread delete failed: %s", exc)
