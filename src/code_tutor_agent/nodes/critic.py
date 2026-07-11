"""critic_node — 宪法约束独立评审节点（D6）。

流程：
    tutor_node 产出提示草稿 → critic_node 评审 → 通过后路由到 wait/submit 或 planner

评审规则：
    - R01: 低等级（<4）下不能泄露完整代码
    - R04: 检测用户挫败情绪
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from langgraph.types import Command

from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# ── R01 代码泄露模式 ──
R01_CODE_LEAK_PATTERNS = [
    r"```\s*python",
    r"class\s+Solution\s*:",
    r"def\s+\w+\s*\(self",
    r"return\s+\w+",
    r"for\s+\w+\s+in\s+range",
    r"while\s+\w+\s*[<>=]",
]

# ── R04 严重情绪关键词 ──
R04_FRUSTRATION_KEYWORDS = [
    "不干了", "放弃", "太难了", "什么鬼", "再也不", "垃圾",
    "frustrated", "give up", "too hard",
]


def critic_node(state: SessionState) -> Command[Literal["wait_for_submit_node", "planner_node", "__end__"]]:
    """Review the latest tutor message, then route to the original destination.

    The tutor_node was modified to route to critic_node first.
    Critic reviews the message, applies R01 sanitization if needed,
    then forwards to the appropriate destination.
    """
    logger.info("▶ critic_node()")

    # ── R01 检查 ──
    if state.tutor_messages:
        last_msg = state.tutor_messages[-1]
        hint_level = state.hint_level

        if hint_level < 4:
            for pattern in R01_CODE_LEAK_PATTERNS:
                if re.search(pattern, last_msg.content, re.IGNORECASE):
                    sanitized = re.sub(
                        r"```[\s\S]*?```",
                        "[系统提示：此等级不可展示代码]",
                        last_msg.content,
                    )
                    state.tutor_messages[-1] = type(last_msg)(
                        role=last_msg.role, content=sanitized, metadata=last_msg.metadata,
                    )
                    logger.warning("R01: sanitized tutor message (pattern=%s, level=%d)", pattern, hint_level)
                    break

    # ── R04 情绪检测 ──
    user_msgs = [m for m in state.tutor_messages if m.role == "user"]
    if user_msgs:
        last_user = user_msgs[-1].content
        if any(kw in last_user for kw in R04_FRUSTRATION_KEYWORDS):
            logger.info("R04: user frustration detected")

    # ── 路由：AC+对抗通过 → planner（下一题），否则 → wait_for_submit（等再提交） ──
    if state.last_verdict == "AC" and state.adversarial_triggered:
        logger.info("critic_node → AC + adversarial passed, goto=planner_node")
        return Command(goto="planner_node")
    else:
        logger.info("critic_node → continue waiting, goto=wait_for_submit_node")
        return Command(goto="wait_for_submit_node")