"""constitutional_guard_node — 宪法 R09/R10 后置守卫。

运行在 tutor_node 之后，扫描 LLM 输出，如果违反宪法约束则替换为合规消息。

检查：
    - R09: 低等级（<4）下不能包含完整代码或几乎完整的实现
    - R10: 不能代写代码（"我帮你写"、"code it for me"等）

与 tutor_node 内部的 _post_guard_scan 是双重防护：
    - tutor_node 内部：prompt 约束防 LLM 主动越界
    - 本节点：独立扫描，防 jailbreak / prompt injection 绕开 prompt 约束
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from langgraph.types import Command

from code_tutor_agent.prompts.tutor import (
    R01_CODE_LEAK_PATTERNS,
    R10_CODE_WRITE_PATTERNS,
)
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# ── R09 代码泄露模式（比 R01 更严格） ──
R09_CODE_PATTERNS = [
    r"```\s*python",
    r"class\s+Solution\s*:",
    r"def\s+\w+\s*\(self",
    r"return\s+\w+",
    r"for\s+\w+\s+in\s+range",
    r"while\s+True",
]

# ── 代写话术（如果 LLM 输出了这些，说明被 jailbreak 了） ──
R10_ANSWER_PATTERNS = [
    "我给你代码", "帮你写", "给你的代码", "你直接复制",
    "code it for me", "here's the code", "copy this",
]

# ── 合规替换消息池 ──
_SANITIZED_MESSAGES = {
    0: "再审审题，再想想从哪里入手？🤔",
    1: "思路方向没问题，但有个细节漏了——再检查一下边界条件？",
    2: "看看输入为空或者只有一个元素的情况？",
    3: "提示：你代码中第 12 行附近的索引在边界处可能会越界 🎯",
}


def _r09_scan(text: str, hint_level: int) -> bool:
    """R09: 低等级下是否包含完整代码片段。"""
    if hint_level >= 4:
        return False  # L4 可以给代码
    for pat in R09_CODE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            logger.warning("R09 violation: pattern=%s at level=%d", pat, hint_level)
            return True
    return False


def _r10_scan(text: str) -> bool:
    """R10: 是否代写。"""
    for pat in R10_ANSWER_PATTERNS:
        if pat in text:
            logger.warning("R10 violation: pattern=%s", pat)
            return True
    # 检查 import 模式（R01 补充）
    for pat in R01_CODE_LEAK_PATTERNS:
        if pat in text:
            logger.warning("R10 (R01 catch): pattern=%s", pat)
            return True
    return False


def constitutional_guard_node(state: SessionState) -> Command[Literal["wait_for_submit_node"]]:
    """Scan the latest tutor message, sanitize if needed, then route to wait_for_submit_node.

    Runs after tutor_node (WA path). The tutor_node already set turn_in_level,
    hint_level, and appended the tutor message. This node only checks and
    potentially replaces the last message, then re-enters wait_for_submit_node
    so the next submission can resume from the interrupt.
    """
    logger.info("▶ constitutional_guard_node() — hint_level=%d", state.hint_level)

    if not state.tutor_messages:
        return Command(goto="wait_for_submit_node")

    last_msg = state.tutor_messages[-1]
    if last_msg.role != "tutor":
        return Command(goto="wait_for_submit_node")

    text = last_msg.content
    hint_level = state.hint_level
    violated = False

    # R09 check
    if _r09_scan(text, hint_level):
        violated = True
        sanitized = _SANITIZED_MESSAGES.get(hint_level, _SANITIZED_MESSAGES[0])
        logger.info("R09: sanitized tutor message at L%d", hint_level)

    # R10 check
    if _r10_scan(text):
        violated = True
        sanitized = (
            "我可以陪你梳理思路，但手得你自己动～"
            "先试着改改，看看哪里可能出问题？"
        )
        logger.info("R10: sanitized tutor message (jailbreak guard)")

    if violated:
        # Replace the last message
        new_msg = type(last_msg)(role=last_msg.role, content=sanitized, metadata=last_msg.metadata)
        new_msgs = list(state.tutor_messages)
        new_msgs[-1] = new_msg
        return Command(goto="wait_for_submit_node", update={"tutor_messages": new_msgs})

    return Command(goto="wait_for_submit_node")