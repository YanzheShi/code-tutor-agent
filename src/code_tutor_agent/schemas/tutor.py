"""Tutor micro-loop schema — L0-L4 MVP rule-based router.

TutorAction:
    CONTINUE   — 同 level 再给一轮辅导
    ESCALATE   — hint_level +1
    RESOLVED   — 用户表示懂了 → 回 solving
    CLARIFY_REQ — 用户卡题意 → 回 clarify（罕见）

EmotionTag:
    confused, frustrated, okay, confident — 从 user_message 用 rule 抽
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


class TutorAction(str, Enum):
    CONTINUE = "continue"
    ESCALATE = "escalate"
    RESOLVED = "resolved"
    CLARIFY_REQ = "clarify_req"


class EmotionTag(str, Enum):
    confused = "confused"
    frustrated = "frustrated"
    okay = "okay"
    confident = "confident"


class TutorRouterInput(BaseModel):
    """Input to the MVP rule-based tutor_router."""
    user_message: str = ""
    hint_level: int = 0
    turns_in_level: int = 0
    diagnosis: Optional[dict] = None
    emotion: EmotionTag = EmotionTag.okay
    max_level: int = 4
    max_turns_per_level: int = 3


class TutorRouterDecision(BaseModel):
    """Output of the tutor_router node."""
    action: TutorAction
    reason: str = ""
    next_hint_level: int = 0
    emotion: EmotionTag = EmotionTag.okay
    diagnosis_refined: Optional[str] = None
    router_model: Literal["rule", "llm"] = "rule"