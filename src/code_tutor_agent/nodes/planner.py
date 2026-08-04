"""规划节点 — 基于用户画像的规则引擎（非 LLM）。

设计（PRD §四 2.2）：
    根据用户画像的 5 维数据，选择最合适的下一题。
    当前为简化版：轮转主题 + 根据熟练度调难度。

双 caller：
    - 被动：critic → planner 走默认 next_in_plan
    - 主动：/next-problem 通过 state.next_preference 传 preference
"""
from __future__ import annotations

import logging
import random
from typing import Literal, Optional

from langgraph.types import Command

from code_tutor_agent.schemas.state import ProblemAttemptRecord, SessionState

logger = logging.getLogger(__name__)

_TOPICS_BY_CATEGORY = [
    ("数组+哈希表", "easy"),
    ("滑动窗口", "medium"),
    ("链表", "easy"),
    ("二分查找", "medium"),
    ("动态规划", "easy"),
]

# ── Tag enum value → 中文 topic（供 same_topic 与 v2 画像选弱 tag 复用）──
_TAG_TO_TOPIC: dict[str, str] = {
    "array_basics": "数组+哈希表",
    "array_two_pointers": "双指针",
    "array_sliding_window": "滑动窗口",
    "array_binary_search": "二分查找",
    "linkedlist_basics": "链表",
    "linkedlist_cycle": "链表",
    "stack_basics": "栈",
    "queue_deque": "队列",
    "dp_1d": "动态规划",
    "dp_multidim": "动态规划",
    "string_basics": "字符串",
    "backtrack": "递归",
    "greedy": "贪心",
    "bit_manip": "位运算",
    "array_sorting": "排序",
    "array_prefix_sum": "前缀和",
}


def _select_topic_by_profile() -> tuple[str, str]:
    """根据用户画像选择 topic 和 difficulty。

    从 DB 读取画像，选熟练度最低的主题，并根据熟练度调整难度。
    无画像数据时走硬编码轮转。
    """
    from code_tutor_agent.db.database import get_profile

    profile = get_profile()
    if profile.attempts == 0:
        return _TOPICS_BY_CATEGORY[0]

    idx = profile.attempts % len(_TOPICS_BY_CATEGORY)
    slug, diff = _TOPICS_BY_CATEGORY[idx]

    # 根据熟练度调整难度
    if profile.proficiency >= 0.8:
        diff = "medium"
    elif profile.proficiency < 0.3 and diff == "medium":
        diff = "easy"

    logger.info(
        "Profile-based selection → topic=%s difficulty=%s (proficiency=%.2f)",
        slug, diff, profile.proficiency,
    )
    return slug, diff


def _select_topic_by_v2_profile() -> Optional[tuple[str, str]]:
    """基于新 per-tag 画像（prof/stab/forget）选择最弱 tag 出题。

    读取 get_user_profile_v2()（默认 user_id="default_v2"）的 per-tag 数据：
        - prof：熟练度 0–1，越低越该练
        - forget：decay 0–1（初始 1.0，越低=忘得越多），越该练
        - stab：方差越大=越不稳定，越该练

    综合打分取"最该练"的 tag，经 _TAG_TO_TOPIC 反查得到中文 topic；
    难度按 prof 推导（<0.3→easy，>=0.8→medium，否则随 stab）。
    无画像 / 画像为空 / tag 无法映射到 topic 时返回 None（由调用方回退旧逻辑）。
    """
    try:
        from code_tutor_agent.db.database import get_user_profile_v2
        profile = get_user_profile_v2()
    except Exception as e:  # noqa: BLE001
        logger.warning("v2 profile read failed: %s", e)
        return None

    if not isinstance(profile, dict):
        return None

    prof: dict[str, float] = profile.get("prof") or {}
    if not prof:
        logger.info("v2 profile has no prof data — fallback")
        return None

    forget: dict[str, dict] = profile.get("forget") or {}
    stab: dict[str, dict] = profile.get("stab") or {}

    # 计算每个 tag 的"该练程度"（越大越优先）
    scored: list[tuple[float, str, str, float]] = []
    for tag, p in prof.items():
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        # 1) 熟练度越低越该练
        weakness = 1.0 - p
        # 2) 遗忘度越高越该练（decay 越低=忘得越多；无 decay 视为 1.0）
        f = forget.get(tag) or {}
        decay = float(f.get("decay", 1.0))
        forgetfulness = 1.0 - decay
        # 3) 稳定性差（方差大）适当加权
        s = stab.get(tag) or {}
        variance = float(s.get("variance", 0.0))
        instability = min(variance, 1.0)
        score = 0.6 * weakness + 0.3 * forgetfulness + 0.1 * instability

        topic = _TAG_TO_TOPIC.get(tag)
        if not topic:
            continue  # 无法映射到可生成 topic 的 tag 跳过
        scored.append((score, tag, topic, p))

    if not scored:
        logger.info("v2 profile tags unmapped to topics — fallback")
        return None

    # 最该练的 tag（score 最大）
    scored.sort(key=lambda x: x[0], reverse=True)
    _, tag, topic, p = scored[0]

    # 难度按熟练度推导
    if p < 0.3:
        difficulty = "easy"
    elif p >= 0.8:
        difficulty = "medium"
    else:
        s = stab.get(tag) or {}
        variance = float(s.get("variance", 0.0))
        difficulty = "medium" if variance < 0.25 else "easy"

    logger.info(
        "v2 profile selection → topic=%s difficulty=%s (tag=%s prof=%.2f score=%.2f)",
        topic, difficulty, tag, p, scored[0][0],
    )
    return topic, difficulty


def _select_topic(
    history: list[ProblemAttemptRecord],
    preference: Optional[str],
) -> tuple[str, str]:
    """纯函数：根据 preference 和 history 选 topic + difficulty。

    Args:
        history: 当前 session 的做题历史
        preference: same_topic / next_in_plan / random

    Returns:
        (topic, difficulty) 如 ("数组+哈希表", "easy")
    """
    if preference == "same_topic" and history:
        last = history[-1]
        # 优先用 history 里的 tags（Tag enum 值）映射到中文 topic
        from_tag = None
        for t in last.tags:
            if t in _TAG_TO_TOPIC:
                from_tag = _TAG_TO_TOPIC[t]
                break
        # 回退用 title 里的 topic
        if from_tag is None:
            # 遍历 _TOPICS_BY_CATEGORY 看 title 是否包含
            for slug, _ in _TOPICS_BY_CATEGORY:
                if slug in last.title:
                    from_tag = slug
                    break
        if from_tag is None:
            from_tag = last.difficulty or "easy"

        # 同 topic 进阶难度
        diff = "medium" if last.difficulty == "easy" else "hard"
        logger.info("same_topic → topic=%s difficulty=%s (from last=%s)", from_tag, diff, last.title)
        return from_tag, diff

    if preference == "random":
        pick = random.choice(_TOPICS_BY_CATEGORY)
        logger.info("random → topic=%s difficulty=%s", pick[0], pick[1])
        return pick

    # 默认 next_in_plan：优先用新 per-tag 画像选最弱 tag，异常/空画像回退旧逻辑
    try:
        v2 = _select_topic_by_v2_profile()
        if v2:
            return v2
    except Exception as e:  # noqa: BLE001
        logger.warning("v2 profile selection failed, fallback to legacy: %s", e)
    return _select_topic_by_profile()


def planner_node(state: SessionState) -> Command[Literal["generator_node", "wait_for_submit_node"]]:
    """Decide the next problem to practice.

    Routing logic:
        - If problem already loaded → skip generation
        - Agent mode with topic/difficulty already set by dialog → use directly
        - First visit / next_in_plan → pick by profile
        - same_topic / random → read from state.next_preference
    """
    logger.info("▶ planner_node() — next_preference=%s", state.next_preference)

    if state.problem:
        logger.info("Problem already loaded — skipping generation, goto=wait_for_submit_node")
        return Command(
            update={"status": "awaiting_submit"},
            goto="wait_for_submit_node",
        )

    # 读 preference（critic→planner 被动路径是 None，/next-problem 主动路径有值）
    preference = state.next_preference

    # Agent 模式：若 dialog 已确定 topic 和 difficulty，直接使用，不走画像选 tag
    if state.mode == "agent" and state.topic and state.difficulty:
        topic, difficulty = state.topic, state.difficulty
        logger.info("Planner (agent) → using dialog result: topic=%s difficulty=%s", topic, difficulty)
    else:
        topic, difficulty = _select_topic(state.problem_history, preference)
        logger.info("Planner → topic=%s difficulty=%s (preference=%s)", topic, difficulty, preference)

    return Command(
        update={
            "status": "awaiting_problem",
            "topic": topic,
            "difficulty": difficulty,
            "error_message": "",
            "next_preference": None,   # 消费掉
            "phase": "solving",
        },
        goto="generator_node",
    )