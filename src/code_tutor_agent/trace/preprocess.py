"""轨迹分析预处理：按题过滤事件 → 生成带「改动前导师提示原文 + 相邻快照 diff」的时间线。

注：problem_id 随每个事件存储在 events_json 内部（由前端采集时带上），
这里只做按题过滤 + 时间线生成；表级 problem_id 列仅作旧数据兜底（见 database.py）。
"""
from __future__ import annotations

import difflib

_MAX_HUNKS = 4          # 单条 edit/submit 最多输出几个 diff hunk
_MAX_LINES_PER_HUNK = 6  # 每个 hunk 最多列出的 -/+ 行数


def _diff_hunks(old_code: str, new_code: str) -> str:
    """相邻两个全量快照的行级 diff（- 旧行 / + 新行，截断防膨胀）。"""
    a = (old_code or "").splitlines()
    b = (new_code or "").splitlines()
    sm = difflib.SequenceMatcher(None, a, b)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_lines = a[i1:i2]
        new_lines = b[j1:j2]
        out.append(f"@@ {tag} L{i1 + 1}-{i2} → L{j1 + 1}-{j2}")
        for ln in old_lines[-_MAX_LINES_PER_HUNK:]:
            out.append("- " + ln)
        for ln in new_lines[-_MAX_LINES_PER_HUNK:]:
            out.append("+ " + ln)
        if len(out) >= _MAX_HUNKS * (_MAX_LINES_PER_HUNK * 2 + 1):
            break
    return "\n".join(out)


def _dialogue_snippet(dlg, max_turns: int = 4, truncate: int = 200) -> str:
    """把事件自带的 dialogue_before 压缩成一行「导师说: ...」。"""
    snippets: list[str] = []
    for d in (dlg or [])[-max_turns:]:
        content = d.get("content") if isinstance(d, dict) else str(d)
        if content:
            snippets.append(content[:truncate])
    return " 导师说: " + " | ".join(snippets) if snippets else ""


def build_dialogue_timeline(events: list[dict], max_events: int = 80, with_diffs: bool = False) -> str:
    """把事件流预处理成时间线，并在每个 edit/submit 处附上其 dialogue_before（导师提示原文）。

    - idle < 1.5s 视为噪声丢弃（与原 build_trace_timeline 一致）；
    - edit/submit 带上"改动前导师轮次原文"（最多取最近 4 条、每条截断 200 字，防 payload 膨胀）；
    - with_diffs=True 时额外输出与上一快照的 diff hunks，让分析 LLM 能还原"代码怎么一步步写出来"。
    """
    picked: list[str] = []
    prev_code = ""
    for ev in events:
        t = ev.get("type")
        if t == "idle":
            idle = ev.get("idleMs", 0) or 0
            if idle < 1500:
                continue
            picked.append(f"[停顿{idle / 1000:.1f}s]")
        elif t in ("edit", "submit"):
            change = ev.get("change") or ""
            label = f"[{t}/{change}]" if change else f"[{t}]"
            label += _dialogue_snippet(ev.get("dialogue_before"))
            if with_diffs:
                code = ev.get("code") or ""
                diff = _diff_hunks(prev_code, code)
                prev_code = code
                if diff:
                    label += f"\n{diff}"
            picked.append(label)
        elif t == "run":
            picked.append("[运行]")
        if len(picked) >= max_events:
            break
    if not picked:
        return "(轨迹无有效事件)"
    return " → ".join(picked)


def _change_size(prev: dict, cur: dict) -> int:
    """相邻两个快照之间的代码变动量（行级 diff 中变化行数，非 equal 块）。"""
    a = (prev.get("code") or "").splitlines()
    b = (cur.get("code") or "").splitlines()
    sm = difflib.SequenceMatcher(None, a, b)
    return sum(
        (i2 - i1) + (j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )


def pick_code_snapshots(events: list[dict], max_snapshots: int = 4, code_limit: int = 1200) -> list[dict]:
    """从 edit/submit 全量快照中选取里程碑快照（首版 / 最大转折 / 终版，防 prompt 膨胀）。

    里程碑策略替代均匀采样：均匀采样会把 token 花在没有信息增量的中间态上，
    首版、最大改动转折点、终版才是复盘最需要的信息。
    返回 [{ts, event, change, code}]——dialogue_before 只在时间线里保留（去重）。
    """
    snaps = [e for e in events if e.get("type") in ("edit", "submit") and e.get("code")]
    if not snaps:
        return []
    if len(snaps) > max_snapshots:
        max_change_idx = max(
            range(1, len(snaps)),
            key=lambda i: _change_size(snaps[i - 1], snaps[i]),
        )
        idxs = {0, len(snaps) // 2, max_change_idx, len(snaps) - 1}
        snaps = [snaps[i] for i in sorted(idxs)]
    out: list[dict] = []
    for e in snaps:
        out.append({
            "ts": e.get("ts"),
            "event": e.get("type"),
            "change": e.get("change") or "",
            "code": (e.get("code") or "")[:code_limit],
        })
    return out


__all__ = ["build_dialogue_timeline", "pick_code_snapshots"]
