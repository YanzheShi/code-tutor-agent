"""Profile 模块核心数据结构。"""
from __future__ import annotations

from typing import Literal, TypedDict

from .tags import Tag


class TagStab(TypedDict):
    """稳定性——最近 N 次提交的滑动窗 + 方差。"""
    window: list[int]       # 最近 N 次 outcome，1=AC 0=Fail，cap=10
    variance: float         # window 方差


class TagForget(TypedDict):
    """遗忘——距离上次练习的时间 + 衰减系数。"""
    last_seen: float        # epoch seconds
    decay: float            # 0–1，初始 1.0


class ErrorFingerprints(TypedDict):
    """错误指纹——全局 + per-tag 统计。"""
    _global: dict[str, int]                 # fp -> count
    per_tag: dict[str, dict[str, int]]      # tag -> {fp -> count}


class AttemptRecord(TypedDict):
    """单道题的提交记录摘要。"""
    count: int
    last_status: Literal["AC", "WA", "TLE", "RE", "Plagiarism"]
    last_code_hash: str | None


class UserProfile(TypedDict):
    """顶层用户画像——5 维数据全部 per-tag。"""
    prof: dict[str, float]                  # tag -> 0–1（由 prof_elo_raw 派生）
    prof_elo_raw: dict[str, float]          # tag -> 1000–4000，初始 1500
    stab: dict[str, TagStab]                # tag -> 稳定性
    forget: dict[str, TagForget]            # tag -> 遗忘
    errors: ErrorFingerprints               # 错误指纹
    attempts: dict[int, AttemptRecord]      # problem_id -> 提交记录
    meta: dict[str, object]                 # 元数据


class ProfileDelta(TypedDict):
    """判题/辅导 node 挂在 session_state['profile_delta'] 上的增量。"""
    tag_primary: str                        # Tag enum value，必填
    prob_elo: int                           # 1200/1500/1800
    outcome: Literal["AC", "WA", "TLE", "RE", "Plagiarism"]
    fingerprints: list[str]                 # 判题规则层打，可空
    misunderstanding_level: int | None      # 辅导 L0-L4，可选