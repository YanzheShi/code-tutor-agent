"""tutor_router node — MVP rule-based L0-L4 routing decision.

Input:  SessionState (reads last user message, hint_level, turns_in_level)
Output: Command with update (action, next_hint_level, turns_in_level)

Routes:
    CONTINUE / ESCALATE → tutor_reply (LLM generate hint)
    RESOLVED           → wait_for_submit_node (back to coding)
    CLARIFY_REQ        → clarify_node (V0.2+)
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.types import Command

from code_tutor_agent.schemas.state import SessionState
from code_tutor_agent.schemas.tutor import (
    EmotionTag,
    TutorAction,
    TutorRouterDecision,
    TutorRouterInput,
)

logger = logging.getLogger(__name__)

# ── 用户说"懂了"/"提交"的关键词 ──
_RESOLVED_KEYWORDS = [
    "我改好了", "提交", "ac", "过了", "懂了", "明白了", "好的",
    "submit", "done", "got it", "i see",
]

# ── 挫败情绪关键词 ──
_FRUSTRATED_KEYWORDS = [
    "不干了", "放弃", "太难了", "什么鬼", "再也不", "垃圾",
    "frustrated", "give up", "too hard", "烦", "不会",
]

# ── 困惑情绪关键词 ──
_CONFUSED_KEYWORDS = [
    "什么意思", "不懂", "没懂", "不理解", "confused", "huh", "what",
]


def _detect_emotion(user_message: str) -> EmotionTag:
    """Simple keyword-based emotion detection."""
    msg_lower = user_message.lower()
    if any(kw in msg_lower for kw in _FRUSTRATED_KEYWORDS):
        return EmotionTag.frustrated
    if any(kw in msg_lower for kw in _CONFUSED_KEYWORDS):
        return EmotionTag.confused
    if any(kw in msg_lower for kw in ("懂了", "明白了", "got it", "i see", "confident")):
        return EmotionTag.confident
    return EmotionTag.okay


def _rule_router(inp: TutorRouterInput) -> TutorRouterDecision:
    """MVP rule-based tutor router (V0.3 切 LLM)."""
    msg = inp.user_message.lower()

    # 1. 用户表示懂了/提交 → RESOLVED
    if any(kw in msg for kw in _RESOLVED_KEYWORDS):
        return TutorRouterDecision(
            action=TutorAction.RESOLVED,
            reason="user indicates resolved or ready to submit",
            next_hint_level=inp.hint_level,
            emotion=inp.emotion,
        )

    # 2. 挫败 + 同 level 已 2 轮 → ESCALATE
    if inp.emotion == EmotionTag.frustrated and inp.hint_level < inp.max_level and inp.turns_in_level >= 2:
        return TutorRouterDecision(
            action=TutorAction.ESCALATE,
            reason=f"frustrated + {inp.turns_in_level} turns at L{inp.hint_level}",
            next_hint_level=inp.hint_level + 1,
            emotion=inp.emotion,
        )

    # 3. 同 level 超上限 → ESCALATE
    if inp.turns_in_level >= inp.max_turns_per_level and inp.hint_level < inp.max_level:
        return TutorRouterDecision(
            action=TutorAction.ESCALATE,
            reason=f"max turns ({inp.turns_in_level}) at L{inp.hint_level}",
            next_hint_level=inp.hint_level + 1,
            emotion=inp.emotion,
        )

    # 4. 默认 → CONTINUE
    return TutorRouterDecision(
        action=TutorAction.CONTINUE,
        reason="default — continue current level",
        next_hint_level=inp.hint_level,
        emotion=inp.emotion,
    )


def tutor_router_node(state: SessionState) -> Command[Literal["tutor_node", "wait_for_submit_node"]]:
    """Run the rule-based router, then route to tutor_reply or back to wait_for_submit.

    Reads:
        - state.tutor_messages[-1] (last user message)
        - state.hint_level
        - state.turns_in_level
    """
    logger.info("▶ tutor_router_node() — hint_level=%d turns=%d", state.hint_level, state.turns_in_level)

    # 找最后一条 user 消息
    user_msg = ""
    for msg in reversed(state.tutor_messages):
        if msg.role == "user":
            user_msg = msg.content
            break

    emotion = _detect_emotion(user_msg)

    inp = TutorRouterInput(
        user_message=user_msg,
        hint_level=state.hint_level,
        turns_in_level=state.turns_in_level,
        emotion=emotion,
    )
    decision = _rule_router(inp)

    logger.info("tutor_router → action=%s reason=%s emotion=%s", decision.action, decision.reason, decision.emotion)

    if decision.action == TutorAction.RESOLVED:
        return Command(
            goto="wait_for_submit_node",
            update={
                "phase": "solving",
                "turns_in_level": 0,
                "last_router_decision": decision.model_dump(),
            },
        )

    # CONTINUE / ESCALATE → tutor_node (LLM reply)
    updates = {
        "last_router_decision": decision.model_dump(),
    }
    if decision.action == TutorAction.ESCALATE:
        updates["hint_level"] = decision.next_hint_level
        updates["turns_in_level"] = 0
    else:
        updates["turns_in_level"] = state.turns_in_level + 1

    return Command(goto="tutor_node", update=updates)