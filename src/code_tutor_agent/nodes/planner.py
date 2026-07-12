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

# ── MVP 硬编码主题轮转（无画像 fallback）──
_TOPIC_QUEUE = [
    ("two_sum", "数组+哈希表", "easy"),
    ("sliding_window", "滑动窗口", "medium"),
    ("linked_list_cycle", "链表", "easy"),
    ("binary_search", "二分查找", "medium"),
    ("dp_fib", "动态规划", "easy"),
]


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
        # 用 tag_primary（Tag enum）映射到中文 topic
        tag_to_topic = {
            "array_basics": "数组",
            "array_two_pointers": "双指针",
            "array_sliding_window": "滑动窗口",
            "array_binary_search": "二分查找",
            "linkedlist_basics": "链表",
            "linkedlist_cycle": "链表",
            "stack_basics": "栈",
            "dp_1d": "动态规划",
            "dp_multidim": "动态规划",
            "string_basics": "字符串",
            "backtrack": "递归",
            "greedy": "贪心",
        }
        # 优先用 history 里的 tags（Tag enum 值）
        from_tag = None
        for t in last.tags:
            if t in tag_to_topic:
                from_tag = tag_to_topic[t]
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

    # 默认 next_in_plan：现有画像轮转逻辑
    return _select_topic_by_profile()


def planner_node(state: SessionState) -> Command[Literal["generator_node", "wait_for_submit_node"]]:
    """Decide the next problem to practice.

    Routing logic:
        - If problem already loaded → skip generation
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