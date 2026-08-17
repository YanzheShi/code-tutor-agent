"""编辑轨迹分析（错误模式画像主 feeder）。

数据流：读 edit_traces 全量事件 → 预处理成紧凑时间线 → 注入历史 6 维先验 →
LLM 增量输出 (dim,tag) deltas → 由编排函数落库（命中衰减 + 加权合并 + 封顶）。

本模块只负责「分析 + 编排落库」，不直接处理前端上传（那是 session API 的职责）。
判题失败补充 feeder（×1.3）也在 run_error_mode_analysis 内统一编排。
详见 docs/error-mode-tracking-design.md。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from code_tutor_agent.config import get_llm
from code_tutor_agent.db.database import get_edit_trace
from code_tutor_agent.profile.weakness import (
    DIM_DISPLAY,
    DIM_KEYS,
    WEAKNESS_TAGS,
    EditTraceAnalysis,
    ErrorModeDelta,
    boost_verdict_deltas,
)

logger = logging.getLogger(__name__)

_USER_ID = "default"  # 单用户模式

# ── 预处理：原始事件 → 给 LLM 看的时间线 ──


def build_trace_timeline(events: list[dict], max_events: int = 60) -> str:
    """把原始事件流预处理成紧凑时间线（过滤噪声、标注卡壳/试错信号）。"""
    if not events:
        return "(无编辑轨迹)"

    picked: list[str] = []
    for ev in events:
        t = ev.get("type")
        if t == "idle":
            idle = ev.get("idleMs", 0) or 0
            if idle < 1500:  # 短停顿是噪声，丢弃
                continue
            picked.append(f"停顿{idle / 1000:.1f}s")
        elif t == "edit":
            change = ev.get("change") or ""
            picked.append(f"编辑[{change}]" if change else "编辑")
        elif t == "run":
            picked.append("运行")
        elif t == "submit":
            picked.append("提交")
        # 其它类型忽略
        if len(picked) >= max_events:
            break

    if not picked:
        return "(轨迹无有效事件)"
    return " → ".join(picked)


def _format_prior(error_modes: dict) -> str:
    """把当前 6 维画像格式化为可读先验文本（供 LLM 校准严重度）。"""
    if not error_modes:
        return "(暂无历史错误模式画像)"
    lines = []
    for dim in DIM_KEYS:
        tags = error_modes.get(dim, {})
        if not tags:
            continue
        ranked = sorted(
            tags.items(),
            key=lambda kv: kv[1].get("count", 0) * kv[1].get("severity", 0),
            reverse=True,
        )
        s = ", ".join(
            f"{tag}(count={it.get('count', 0)},sev={it.get('severity', 0)})"
            for tag, it in ranked
        )
        lines.append(f"- {DIM_DISPLAY[dim]}: {s}")
    return "\n".join(lines) if lines else "(暂无历史错误模式画像)"


_PROMPT = """你是一个编程错误模式分析器。基于一次做题的【编辑轨迹】与【最终代码】，识别用户暴露出的错误模式，按给定的 6 个维度输出增量。

## 6 个维度与合法小项（tag 必须精确命中其一）
{catalog}

## 用户历史错误模式画像(先验，用于校准严重度、避免重复表述)
{prior}

## 本题信息
- 知识点: {topic}
- 题目描述(节选): {description}

## 最终提交的代码
```python
{final_code}
```

## 编辑轨迹时间线(反映"怎么做出来的":卡壳/反复修改/试错/提交前自查)
{timeline}

## 任务
1. 只输出本次轨迹与代码中**新暴露**的错误模式(增量)。若先验已存在同维度同小项且本次无更强证据，不要重复。
2. dim 必须是 6 维之一；tag 必须精确命中上述对应维度的合法小项。
3. 每个模式给 delta_count(本次暴露次数,整数≥0)与 severity(0~1,越严重越接近 1)。
4. evidence 用一句中文简述证据来源(来自轨迹或代码)。
5. 若轨迹与代码均未暴露明显错误模式，返回空 deltas。

只输出结构化结果，不要额外解释。
"""


def analyze_edit_trace(
    session_id: str,
    *,
    topic: str = "",
    description: str = "",
    final_code: str = "",
    prior_error_modes: Optional[dict] = None,
) -> EditTraceAnalysis:
    """分析某会话的编辑轨迹，产出错误模式增量（非致命，失败返回空分析）。

    Args:
        session_id: 会话 id（用于读取 edit_traces 全量事件）
        topic/description/final_code: 本题上下文与最终代码（供 LLM 推断）
        prior_error_modes: 当前 DBProfile.error_modes（先验，用于校准）
    """
    prior_error_modes = prior_error_modes or {}
    try:
        events = get_edit_trace(session_id)
    except Exception as exc:
        logger.warning("analyze_edit_trace: read trace failed for %s: %s", session_id, exc)
        return EditTraceAnalysis(deltas=[])

    if not events:
        logger.info("analyze_edit_trace: no events for %s, skip", session_id)
        return EditTraceAnalysis(deltas=[])

    timeline = build_trace_timeline(events)
    prior_text = _format_prior(prior_error_modes)

    catalog_lines = [
        f"- {dim} ({DIM_DISPLAY[dim]}): {', '.join(WEAKNESS_TAGS[dim])}"
        for dim in DIM_KEYS
    ]
    catalog = "\n".join(catalog_lines)

    prompt = _PROMPT.format(
        catalog=catalog,
        prior=prior_text,
        topic=topic or "(未知)",
        description=(description or "")[:600],
        final_code=(final_code or "")[:4000],
        timeline=timeline,
    )

    try:
        llm = get_llm(purpose="edit-trace")
        # 默认 method（json_schema，response_format 路径）：sensenova 网关在思考模式下
        # 不允许 tool_choice 指定具体函数，显式 function_calling 会 400（2026-08-16 实测）；
        # 与 judge/problem/dialog 等其它结构化输出调用点保持一致，走默认 json_schema。
        structured = llm.with_structured_output(EditTraceAnalysis)
        result = structured.invoke(prompt)
        if not isinstance(result, EditTraceAnalysis):
            result = EditTraceAnalysis(**(result if isinstance(result, dict) else {}))
        logger.info("analyze_edit_trace(%s) → %d deltas", session_id, len(result.deltas))
        return result
    except Exception as exc:
        logger.error("analyze_edit_trace: LLM failed for %s: %s", session_id, exc)
        return EditTraceAnalysis(deltas=[])


# ── 判题失败补充 feeder：verdict → (dim, tag) 启发式映射 ──


def judge_failure_to_tags(verdict: str) -> list[tuple[str, str]]:
    """把判题失败 verdict 映射为应当强化的 (dim, tag)。

    仅覆盖高置信信号；WA 这类根因多样的情形，用最常见的边界/空处理作兜底强化
    （隐藏用例失败而样例通过时，轨迹分析可能漏判）。

    注：verdict 由执行引擎客观归约（见 agents/agent_judge.py 的 _deterministic_verdict），
    字面值只有 ``"TLE"`` / ``"RE"`` / ``"WA"`` / ``"AC"``，直接按字面值匹配即可。
    """
    tags: list[tuple[str, str]] = []
    v = (verdict or "").upper()
    if v == "TLE":
        tags.append(("perf", "tle_brute"))
    elif v == "RE":
        tags.append(("correctness", "none_handling"))
    elif v in ("WA", "WRONG"):
        tags.append(("correctness", "boundary"))
    return tags


# ── 编排落库（fire-and-forget 入口调用）──


def run_error_mode_analysis(
    session_id: str,
    *,
    topic: str,
    description: str,
    final_code: str,
    verdict: str,
    judge_failure_tags: Optional[list[tuple[str, str]]] = None,
) -> None:
    """编排：编辑轨迹增量(基准) + 判题失败补充 feeder(×1.3) → 落库。非致命。

    一次提交 = 一个时间步：两个 feeder 的 deltas **合并成一次** ``apply_deltas``
    调用，避免对所有已有 tag 施加两次时间衰减（见设计文档 §7 修正说明）。
    """
    from code_tutor_agent.db.database import apply_error_mode_deltas, get_profile

    # 先验（供 LLM 校准严重度）
    try:
        prior = get_profile(_USER_ID).error_modes
    except Exception:
        prior = {}

    # 1) 编辑轨迹基准 deltas（不 boost）
    base_deltas: list[ErrorModeDelta] = []
    try:
        analysis = analyze_edit_trace(
            session_id,
            topic=topic,
            description=description,
            final_code=final_code,
            prior_error_modes=prior,
        )
        base_deltas = list(analysis.deltas)
    except Exception as exc:
        logger.error("run_error_mode_analysis: edit-trace stage failed: %s", exc)

    # 2) 判题失败补充 feeder（×1.3）：先整体 boost，再并入同一批 deltas
    merged: list[ErrorModeDelta] = list(base_deltas)
    if judge_failure_tags:
        judge_deltas = [
            ErrorModeDelta(
                dim=d, tag=t, delta_count=1, severity=0.7,
                evidence=f"判题失败(verdict={verdict})",
            )
            for (d, t) in judge_failure_tags
        ]
        merged.extend(boost_verdict_deltas(judge_deltas))

    # 3) 单次落库（一个时间步）
    if merged:
        try:
            apply_error_mode_deltas(_USER_ID, merged, verdict_boost=False)
        except Exception as exc:
            logger.error("run_error_mode_analysis: apply stage failed: %s", exc)


def fire_and_forget_error_mode_analysis(
    session_id: str,
    *,
    topic: str,
    description: str,
    final_code: str,
    verdict: str,
    judge_failure_tags: Optional[list[tuple[str, str]]] = None,
) -> None:
    """fire-and-forget 启动错误模式分析。

    判题节点运行在 ``asyncio.to_thread`` 的 worker 线程中，无运行中的事件循环，
    且分析含阻塞式 LLM 调用，因此用守护线程承载（即 fire-and-forget 的线程实现）。
    非致命：线程内任何异常只记日志，不影响主流程。
    """
    def _worker():
        try:
            run_error_mode_analysis(
                session_id,
                topic=topic,
                description=description,
                final_code=final_code,
                verdict=verdict,
                judge_failure_tags=judge_failure_tags,
            )
        except Exception as exc:  # 兜底，绝不让线程异常冒泡
            logger.error("fire_and_forget_error_mode_analysis worker failed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    logger.info("fire_and_forget_error_mode_analysis started for session=%s verdict=%s", session_id, verdict)
