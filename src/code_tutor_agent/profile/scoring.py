"""单次行为打分 —— 纯函数，无 IO。"""
from __future__ import annotations

from .schema import (
    ErrorFingerprints,
    ProfileDelta,
    UserProfile,
)

# ── 常量 ──
K = 64                       # ELO K-factor
ELO_INIT = 1500              # 新 tag 首次出现时的 raw elo
ELO_MIN, ELO_MAX = 1000, 4000  # 压到 0–1 的区间
DECAY_HOURS = 168            # 一周，forget 线性衰减分母
STAB_WINDOW_CAP = 10         # stab.window 最大长度


def _variance(arr: list[int]) -> float:
    if len(arr) <= 1:
        return 0.0
    mu = sum(arr) / len(arr)
    return sum((x - mu) ** 2 for x in arr) / len(arr)


def apply_delta(
    profile: UserProfile,
    delta: ProfileDelta,
    problem_id: int,
    code_hash: str | None,
    now: float,
) -> UserProfile:
    """纯函数：输入旧 profile + delta，返回新 profile。

    Args:
        profile: 当前用户画像（可修改副本）
        delta: 本轮的增量数据
        problem_id: 题目 ID
        code_hash: 用户代码哈希（判题 node 计算）
        now: 当前 epoch seconds

    Returns:
        更新后的 UserProfile。
    """
    tag = delta["tag_primary"]
    S = 1 if delta["outcome"] == "AC" else 0

    # ════════════════════════════════════════
    # ① prof (ELO)
    # ════════════════════════════════════════
    raw = dict(profile.get("prof_elo_raw", {}))
    old_elo = raw.get(tag, ELO_INIT)
    E = 1 / (1 + 10 ** ((delta["prob_elo"] - old_elo) / 400))
    new_elo = old_elo + K * (S - E)
    new_elo = max(ELO_MIN, min(ELO_MAX, new_elo))
    raw[tag] = new_elo
    profile["prof_elo_raw"] = raw
    profile["prof"] = dict(profile.get("prof", {}))
    profile["prof"][tag] = (new_elo - ELO_MIN) / (ELO_MAX - ELO_MIN)

    # ════════════════════════════════════════
    # ② stab
    # ════════════════════════════════════════
    stab = dict(profile.get("stab", {}))
    if tag not in stab:
        stab[tag] = {"window": [], "variance": 0.0}
    w = list(stab[tag]["window"])
    w.append(S)
    if len(w) > STAB_WINDOW_CAP:
        w.pop(0)
    stab[tag] = {"window": w, "variance": _variance(w)}
    profile["stab"] = stab

    # ════════════════════════════════════════
    # ③ forget
    # ════════════════════════════════════════
    forget = dict(profile.get("forget", {}))
    # 主动：刚练的 tag 重置
    forget[tag] = {"last_seen": now, "decay": 1.0}
    # 被动：其他 tag 衰减
    for t, f in forget.items():
        if t == tag:
            continue
        hours = (now - f["last_seen"]) / 3600
        f["decay"] = max(0.0, 1.0 - hours / DECAY_HOURS)
    profile["forget"] = forget

    # ════════════════════════════════════════
    # ④ errors
    # ════════════════════════════════════════
    errors: ErrorFingerprints = {
        "_global": dict(profile.get("errors", {}).get("_global", {})),
        "per_tag": dict(profile.get("errors", {}).get("per_tag", {})),
    }
    for fp in delta["fingerprints"]:
        errors["_global"][fp] = errors["_global"].get(fp, 0) + 1
        tag_fps = dict(errors["per_tag"].get(tag, {}))
        tag_fps[fp] = tag_fps.get(fp, 0) + 1
        errors["per_tag"][tag] = tag_fps
    profile["errors"] = errors

    # ════════════════════════════════════════
    # ⑤ attempts
    # ════════════════════════════════════════
    attempts = dict(profile.get("attempts", {}))
    a = dict(attempts.get(problem_id, {
        "count": 0, "last_status": "", "last_code_hash": None,
    }))
    a["count"] += 1
    a["last_status"] = delta["outcome"]
    a["last_code_hash"] = code_hash
    attempts[problem_id] = a
    profile["attempts"] = attempts

    # ════════════════════════════════════════
    # meta
    # ════════════════════════════════════════
    profile["meta"] = dict(profile.get("meta", {}))
    profile["meta"]["updated_at"] = now
    profile["meta"]["schema_version"] = "mvp@1"

    return profile