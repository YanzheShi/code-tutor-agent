"""Planner node — MVP: hardcoded rules (no LLM).

The planner sits at the START of each round and decides *what* to
practice next.  For D1 it emits a fixed topic/difficulty; D5 will
replace this with a real rule engine reading user profiles.
"""

from __future__ import annotations

import logging

from langgraph.types import Command

from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# ── Hardcoded topic rotation for MVP ──
_TOPIC_QUEUE = [
    ("two_sum", "数组+哈希表", "easy"),
    ("sliding_window", "滑动窗口", "medium"),
    ("linked_list_cycle", "链表", "easy"),
    ("binary_search", "二分查找", "medium"),
    ("dp_fib", "动态规划", "easy"),
]


def planner_node(state: SessionState) -> Command:
    logger.info("▶ planner_node()")
    """Decide the next problem to practice (hardcoded MVP).

    Routing logic:
        - If problem already loaded (from LeetCode import or existing pool) → skip generation
        - First session visit → pick from the hardcoded topic queue.
        - After an AC → advance to the next topic in the queue.
        - After a give-up / error → stay on the same topic but easier.

    Args:
        state: Current session state (submissions history, verdict, etc.)

    Returns:
        Command with ``goto`` and updated state fields.
    """
    # ── If a problem is already loaded, go straight to awaiting_submit ──
    if state.problem:
        logger.info("Problem already loaded — skipping generation, goto=wait_for_submit_node")
        return Command(
            update={"status": "awaiting_submit"},
            goto="wait_for_submit_node",
        )

    # Count how many problems this session has tackled (unique per AC signal)
    completed = sum(
        1 for s in state.submissions
        if any(r.status == "AC" for r in s.judge_results)
    )
    idx = min(completed, len(_TOPIC_QUEUE) - 1)
    slug, topic, difficulty = _TOPIC_QUEUE[idx]

    logger.info(
        "Planner → topic=%s difficulty=%s (completed=%d, idx=%d)",
        topic, difficulty, completed, idx,
    )

    # Update state via Command — LG 1.x pattern
    logger.debug("Returning Command with goto=%s", 'return Command(')
    return Command(
        update={
            "status": "awaiting_problem",
            "error_message": "",
        },
        goto="generator_node",
    )