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


# ── LLM router prompt ──
_ROUTER_LLM_PROMPT = """你是一个编程辅导的路由决策专家。根据用户当前的情况，决定下一步的辅导策略。

用户当前状态：
- hint_level（提示等级）: {hint_level}（0=模糊→4=近答案）
- 同 level 已辅导轮数: {turns_in_level}
- 用户情绪: {emotion}

用户最近的消息：{user_message}

可选策略：
1. **continue** — 同 level 再给一轮辅导，用户还没完全理解
2. **escalate** — 升级 hint_level +1，因为用户在同 level 已经卡了很久
3. **resolved** — 用户表示懂了/想提交了，回到写代码模式
4. **clarify_req** — 用户对题意有疑问，需要澄清

输出 JSON 格式：
{{
    "action": "continue|escalate|resolved|clarify_req",
    "reason": "一句话解释为什么",
    "next_hint_level": <int, escalate 时 = old+1，否则 = old>,
    "emotion": "confused|frustrated|okay|confident"
}}
"""


def _llm_router(inp: TutorRouterInput) -> TutorRouterDecision | None:
    """LLM-based router decision. Returns None on failure (fallback to rule)."""
    from code_tutor_agent.config import get_llm
    from langchain_core.prompts import ChatPromptTemplate

    try:
        llm = get_llm(purpose="tutor-router")
        prompt = ChatPromptTemplate.from_messages([("human", _ROUTER_LLM_PROMPT)])
        result = (prompt | llm).invoke({
            "hint_level": inp.hint_level,
            "turns_in_level": inp.turns_in_level,
            "emotion": inp.emotion.value,
            "user_message": inp.user_message[:500],
        })
        text = result.content if hasattr(result, "content") else str(result)
        import json
        # 提取 JSON 块
        if m := __import__('re').search(r'\{[^}]+\}', text, __import__('re').DOTALL):
            data = json.loads(m.group(0))
            action = TutorAction(data.get("action", "continue"))
            return TutorRouterDecision(
                action=action,
                reason=data.get("reason", "")[:100],
                next_hint_level=data.get("next_hint_level", inp.hint_level),
                emotion=EmotionTag(data.get("emotion", "okay")),
                router_model="llm",
            )
    except Exception as exc:
        logger.warning("LLM router failed: %s — falling back to rule", exc)
    return None


def tutor_router_node(state: SessionState) -> Command[Literal["tutor_node", "wait_for_submit_node"]]:
    """Run the LLM router (fallback to rule), then route.

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

    # 先试 LLM，失败则回退规则
    decision = _llm_router(inp)
    if decision is None:
        decision = _rule_router(inp)
        logger.info("tutor_router → using RULE (LLM failed)")
    else:
        logger.info("tutor_router → using LLM")

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