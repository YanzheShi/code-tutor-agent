"""规划节点 — 基于用户画像的规则引擎（非 LLM）。

设计（PRD §四 2.2）：
    根据用户画像的 5 维数据，选择最合适的下一题。
    当前为简化版：轮转主题 + 根据熟练度调难度。
"""
from __future__ import annotations

import logging
from typing import Literal

from langgraph.types import Command

from code_tutor_agent.schemas.state import SessionState

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


def planner_node(state: SessionState) -> Command[Literal["generator_node", "wait_for_submit_node"]]:
    """Decide the next problem to practice.

    Routing logic:
        - If problem already loaded → skip generation
        - First visit → pick by profile or hardcoded queue.
        - After AC → advance to next topic.
    """
    logger.info("▶ planner_node()")

    if state.problem:
        logger.info("Problem already loaded — skipping generation, goto=wait_for_submit_node")
        return Command(
            update={"status": "awaiting_submit"},
            goto="wait_for_submit_node",
        )

    # 尝试按画像选择
    topic, difficulty = _select_topic_by_profile()

    logger.info("Planner → topic=%s difficulty=%s", topic, difficulty)

    return Command(
        update={
            "status": "awaiting_problem",
            "error_message": "",
        },
        goto="generator_node",
    )