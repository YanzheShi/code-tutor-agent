"""轨迹分析前的程序化抽取层：把密集全量事件蒸馏成喂给 LLM 的核心变化。

零 LLM、确定性、纯函数。由 first_round_analysis 在分析前调用。

设计背景（见 docs/edit-trace-fullsnapshot-design.md）：
- 采集层改为「每次 edit/run/submit 全量存 code」（不再存 diff 链），存储真相即全量代码，
  不再有 diff 链断裂导致的累积错位风险。
- 但全量事件密集（30 分钟约 300-800 条），不能直接喂 LLM，必须在此层程序化蒸馏：
  1) 排序 + 去噪 + 去重（无增量的 edit 跳过）；
  2) 时间桶合并（连续打字波次 <400ms 只留首尾，中间态已全量落库不丢真相）；
  3) 核心变化时间线（相邻保留的 edit/submit 用 preprocess._diff_hunks 算行级 diff）；
  4) 里程碑快照（全量方案下不再稀疏）；
  5) 结构化卡壳段 stuck_segments（卡壳锚定代码 + 卡前刚收到的导师提示）；
  6) 超限裁剪：两两归并相邻事件（条数减半、时间跨度不减），而非前缀截断。

复用 preprocess._diff_hunks / build_dialogue_timeline / pick_code_snapshots —— 下游分析契约不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .preprocess import (
    _diff_hunks,
    build_dialogue_timeline,
    pick_code_snapshots,
)

_EDIT_BUCKET_MS = 400        # 相邻 edit 间隔 < 此值视为同一打字波次，波次内只留首尾
_IDLE_NOISE_MS = 1500        # idle < 此值视为噪声丢弃（与 build_dialogue_timeline 口径一致）
_MAX_EVENTS = 80             # 时间线事件上限；超出时两两归并（保时间跨度）
_MAX_SNAPSHOTS = 4
_SNAPSHOT_CODE_LIMIT = 1200
_TOKEN_BUDGET = 12_000       # 同 trace/agent.py:_FIRST_ROUND_TOKEN_BUDGET


@dataclass
class ExtractionResult:
    timeline_text: str
    snapshots: list[dict]
    event_count_raw: int
    event_count_kept: int
    metrics: dict
    stuck_segments: list[dict] = None   # A3：结构化卡壳段


def _merge_time_buckets(kept: list[dict], bucket_ms: int) -> list[dict]:
    """连续 edit 波次（间隔 < bucket_ms）内只保留【首】+【末】，丢弃中间态。

    中间态代码已全量落库（真相不丢），时间线只关心波次净变化（首→末的 diff）。
    run / submit / idle 作为"波次边界"天然打断合并。

    A4：thrash 检测——若波次内存在 ≥2 条互不相同的 code 快照，说明"写一段→删→又写"
    的快速来回（典型犹豫/挣扎），标记 `thrash: True`，不吞信号（由上层累加进 metrics）。
    """
    out: list[dict] = []
    i = 0
    n = len(kept)
    while i < n:
        e = kept[i]
        if e.get("type") != "edit":
            out.append(e)
            i += 1
            continue
        bucket = [e]
        j = i
        while (
            j + 1 < n
            and kept[j + 1].get("type") == "edit"
            and (kept[j + 1].get("ts", 0) - kept[j].get("ts", 0)) < bucket_ms
        ):
            j += 1
            bucket.append(kept[j])
        out.append(bucket[0])
        if len(bucket) > 1:
            out.append(bucket[-1])   # 丢弃中间态，保留波次末（= 该段最终代码）
        # A4：波次内出现 ≥2 个不同 code → thrash（挣扎/反复）
        distinct_codes = {b.get("code") for b in bucket if b.get("code") is not None}
        thrash = len(distinct_codes) >= 2
        out[-1] = {**out[-1], "thrash": thrash}   # 末条带 thrash 标记
        i = j + 1
    return out


def _event_parts(ev: dict) -> list[dict]:
    """取出事件包含的原始子事件；已归并的复合事件展平，避免多层嵌套。"""
    if ev.get("type") == "composite":
        return list(ev.get("parts") or [])
    return [ev]


def _coalesce_pairs(events: list[dict]) -> list[dict]:
    """把相邻事件两两归并成一条复合事件：条数减半，时间跨度不变。

    复合事件保留各子事件的标签与 dialogue_before（渲染层拼接 + 去重），
    code 取跨度内最后一条带码事件，diff 由渲染层按「上一条保留事件 → 跨度末端」
    重算，因此只损失单步 diff 粒度，不会像前缀截断那样整段丢掉会话尾部。
    """
    out: list[dict] = []
    i = 0
    n = len(events)
    while i < n:
        a = events[i]
        if i + 1 >= n:
            out.append(a)
            break
        b = events[i + 1]
        parts = _event_parts(a) + _event_parts(b)
        code = None
        for p in reversed(parts):
            if p.get("code") is not None:
                code = p["code"]
                break
        out.append({
            "type": "composite",
            "ts": b.get("ts", a.get("ts")),
            "parts": parts,
            "code": code,
        })
        i += 2
    return out


def _render_timeline(events: list[dict]) -> str:
    """渲染时间线；事件已按上限归并，这里不再做前缀截断。"""
    return build_dialogue_timeline(events, max_events=max(len(events), 1), with_diffs=True)


def _estimate_tokens(timeline_text: str, snapshots: list[dict]) -> int:
    """粗略估算：中文约 1 token/字符；代码英文约 3 字符/token。取保守上限。"""
    code_chars = sum(len(s.get("code", "")) for s in snapshots)
    return len(timeline_text) // 2 + code_chars // 3


def extract_for_analysis(events: list[dict], max_tokens: int = _TOKEN_BUDGET) -> ExtractionResult:
    """把全量事件流蒸馏成喂 LLM 的结构化输入。

    Args:
        events: 已 reconstruct 为全量 code 的事件列表
                （edit/run/submit 带 code，idle 可能带 code_at_pause + dialogue_before）。
        max_tokens: token 预算上限，超预算时对时间线继续两两归并（保时间跨度）。

    Returns:
        ExtractionResult: timeline_text / snapshots / 计数器 / 结构化卡壳段。
    """
    raw = list(events)
    # 1) 排序（与 reconstruct 一致，保证稳定序）
    evs = sorted(raw, key=lambda e: (e.get("ts", 0), e.get("seq", 0) or 0))

    # 2) 去噪 + 去重：run/submit/idle 全留；edit 仅留 code 相对上一条带码事件有变化者
    kept: list[dict] = []
    last_code: Optional[str] = None
    for e in evs:
        t = e.get("type")
        if t == "idle":
            if (e.get("idleMs", 0) or 0) < _IDLE_NOISE_MS:
                continue
            kept.append(e)
        elif t in ("run", "submit"):
            kept.append(e)
            if e.get("code") is not None:
                last_code = e["code"]
        elif t == "edit":
            code = e.get("code")
            if code is None:
                continue
            if code == last_code:
                continue  # 无增量（停顿中间态 / 纯 hover 触发）—— 跳过
            kept.append(e)
            last_code = code
        # 其它未知类型忽略

    # 3) 时间桶合并（连续打字波次只留首尾）
    merged = _merge_time_buckets(kept, _EDIT_BUCKET_MS)

    # 4) 核心变化时间线（相邻保留的 edit/submit 用 _diff_hunks 算行级 diff）
    #    超出条目上限时两两归并：保留整个会话跨度，只降低单条粒度
    render_events = merged
    while len(render_events) > _MAX_EVENTS:
        render_events = _coalesce_pairs(render_events)
    timeline_text = _render_timeline(render_events)

    # 5) 里程碑快照（全量方案下每个 edit/submit 都有 code，不再稀疏——修旧 starvation）
    snapshots = pick_code_snapshots(merged, max_snapshots=_MAX_SNAPSHOTS, code_limit=_SNAPSHOT_CODE_LIMIT)

    # 6) token 预算裁剪：继续两两归并，条数与 diff 数逐轮减半；至少保留 1 条
    while _estimate_tokens(timeline_text, snapshots) > max_tokens and len(render_events) > 1:
        render_events = _coalesce_pairs(render_events)
        timeline_text = _render_timeline(render_events)

    # ★ A3：结构化卡壳段 —— 遍历 kept 的 idle 事件，关联"卡前/卡后代码 + 卡时对话"
    stuck_segments: list[dict] = []
    for idx, e in enumerate(kept):
        if e.get("type") != "idle":
            continue
        # 卡前最后一条带码事件（含 pause 自身若带 code_at_pause）
        pre = None
        for k in range(idx - 1, -1, -1):
            c = kept[k].get("code") or kept[k].get("code_at_pause")
            if c is not None:
                pre = c
                break
        # 卡后第一条带码事件
        post = None
        for k in range(idx + 1, len(kept)):
            c = kept[k].get("code") or kept[k].get("code_at_pause")
            if c is not None:
                post = c
                break
        seg = {
            "ts": e.get("ts"),
            "idleMs": e.get("idleMs", 0),
            "level": e.get("level"),
            "away": e.get("away", False),
            "code_at_pause": e.get("code_at_pause"),   # A1：卡壳锚定的代码（已落库）
            "pre_code": (pre or "")[:_SNAPSHOT_CODE_LIMIT] if pre else None,
            "post_code": (post or "")[:_SNAPSHOT_CODE_LIMIT] if post else None,
            "dialogue_before": e.get("dialogue_before"),  # A2：卡壳前刚收到的提示原文
        }
        if not seg["code_at_pause"] and pre:
            seg["code_at_pause"] = pre   # 兜底：前驱 edit 代码即卡壳时状态
        stuck_segments.append(seg)

    # A4：thrash 统计（合并末条带 thrash 标记的事件数）
    thrash_count = sum(1 for e in merged if e.get("thrash"))

    metrics = {
        "edit_count": sum(1 for e in kept if e.get("type") == "edit"),
        "run_count": sum(1 for e in kept if e.get("type") == "run"),
        "submit_count": sum(1 for e in kept if e.get("type") == "submit"),
        "idle_count": sum(1 for e in kept if e.get("type") == "idle"),
        "stuck_count": len(stuck_segments),
        "thrash_count": thrash_count,
        "event_count_raw": len(raw),
        "event_count_kept": len(kept),
        "event_count_merged": len(merged),
        "event_count_rendered": len(render_events),
    }

    return ExtractionResult(
        timeline_text=timeline_text,
        snapshots=snapshots,
        event_count_raw=len(raw),
        event_count_kept=len(kept),
        metrics=metrics,
        stuck_segments=stuck_segments,
    )
