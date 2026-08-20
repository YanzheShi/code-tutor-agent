"""过渡压缩：把分析线程对话压成 ≤500 字 / ≤10 条摘要（双落点源）。

- 可见卡：主聊天渲染 summary_text + bullets；
- 主上下文注入：由 next-problem 上下文管线读取本摘要（见 session 路由 summarize 端点返回）。
线程 transcript 读自 trace_threads 表（服务重启后仍可压缩）。不回灌 profile / memory。
"""
from __future__ import annotations

import logging

from code_tutor_agent.config import get_llm
from code_tutor_agent.db.database import get_analysis_result, save_trace_summary
from code_tutor_agent.trace.agent import _invoke_with_retry, load_thread_messages
from code_tutor_agent.trace.schemas import TraceSummary

logger = logging.getLogger(__name__)

_PROMPT = """你正在把一次「做题轨迹分析」的多轮对话压缩成一份简短摘要，用于：
1) 在主聊天里给用户看一张「轨迹分析摘要」卡；
2) 作为只读上下文注入下一题的导师对话（让导师知道用户暴露的薄弱点）。

要求：
- summary_text 不超过 500 字；
- bullets 不超过 10 条要点；
- 必须保留：主要薄弱点、哪些是被导师提示才改对的（hint 依赖）、autonomy 信号（独立改对率 / 依赖提示的弱点）；
- 丢弃 diff 细节与逐轮原文，只留结论与建议。

只输出结构化摘要。"""


def _estimate_tokens(summary: TraceSummary) -> int:
    """粗估摘要产物 token 数（CJK≈1 token/字，ASCII≈4 字/token，取中间系数 2）。"""
    text = summary.summary_text or ""
    text += "".join(summary.bullets or [])
    text += summary.autonomy.model_dump_json()
    text += "".join(summary.hint_dependence or [])
    return max(1, int(len(text) / 2))


def _render_analysis_head(analysis: dict) -> str:
    """把首轮结构化结论渲染成精简块（替代线程里的大 JSON，只留对下一题导师有用的结论）。"""
    seg = ["## 首轮分析要点（结构化字段）"]
    summary = analysis.get("summary") or ""
    if summary:
        seg.append(f"总评：{summary}")
    weakness = analysis.get("weakness_tags") or []
    if weakness:
        items = [
            f"- {w.get('tag', '')}（severity={w.get('severity', 0)}，"
            f"trigger={w.get('trigger', 'self')}）"
            for w in weakness[:5]
            if w.get("tag")
        ]
        if items:
            seg.append("薄弱点：\n" + "\n".join(items))
    autonomy = analysis.get("autonomy") or {}
    if autonomy.get("self_fix_rate") is not None:
        seg.append(f"独立改对率：{autonomy['self_fix_rate']}")
    hint_dep = autonomy.get("hint_dependent_weaknesses") or []
    if hint_dep:
        seg.append("依赖提示的弱点：" + "、".join(str(h) for h in hint_dep))
    return "\n".join(seg) if len(seg) > 1 else ""


def summarize_thread(session_id: str, problem_id: str, transition_action: str) -> TraceSummary:
    """读当前题分析线程 transcript → LLM 压成 ≤500 字/10 条 → 落 TraceSummary。

    压缩输入只含「追问 + 答复」原文（尾部保留，最近信息密度最高）：
    - system 是固定指令，不进输入；
    - 首轮结论 AI 消息由 analysis_results 表结构化保存，用精简块替代大 JSON。
    线程一旦超长，裁掉的是最旧内容而非最近追问（原头部截断方向相反）。
    """
    messages = load_thread_messages(session_id, problem_id)
    if not messages:
        return TraceSummary(summary_text="（本题未做轨迹分析，无摘要）")

    parts: list[str] = []
    first_ai_seen = False
    for m in messages:
        if m.type == "system":
            continue
        if m.type == "ai" and not first_ai_seen:
            first_ai_seen = True
            continue
        content = m.content
        if isinstance(content, list):
            content = "".join(str(p) for p in content)
        parts.append(f"{m.type}: {content}")

    analysis = get_analysis_result(session_id, problem_id)
    if analysis:
        head = _render_analysis_head(analysis)
        if head:
            parts.insert(0, head)

    transcript = "\n".join(parts)
    if not transcript.strip():
        return TraceSummary(summary_text="（本题未做多轮追问，无摘要）")

    full = _PROMPT + "\n\n## 分析对话记录\n" + transcript[-8000:]
    try:
        llm = get_llm(purpose="edit-trace")
        structured = llm.with_structured_output(TraceSummary)
        result = _invoke_with_retry(lambda: structured.invoke(full))
        if not isinstance(result, TraceSummary):
            result = TraceSummary(**(result if isinstance(result, dict) else {}))
        try:
            save_trace_summary(
                session_id, problem_id, transition_action,
                result.model_dump(), token_est=_estimate_tokens(result),
            )
        except Exception as exc:
            logger.warning("save_trace_summary failed: %s", exc)
        return result
    except Exception as exc:
        logger.error("summarize_thread LLM failed: %s", exc)
        return TraceSummary(summary_text="（摘要生成失败）")
