"""Tutor node — L0-L4 渐进辅导决策树 + LLM 提示生成 + 宪法 post-guard (D4)。

**架构**（面试用）：

    决策树（纯规则，零 LLM）→ 决定 hint_level
        ↓
    LLM 调用（含宪法 prompt guard）→ 生成提示文本
        ↓
    Post-guard（纯关键词规则）→ 防 jailbreak / 代码泄露

为什么决策树是纯规则？
    — 提示等级是"教学策略"问题，不是"语言理解"问题。
      规则决策：方向对→跳级、重复错→插讲解、情绪急→安抚，
      这些是确定性逻辑，LLM 做反而容易过拟合或遗漏。
    为什么 hint 生成是 LLM？
    — 同样 L1 的提示，对"忘了边界"和"忘了哈希表"的用户完全不同。
      LLM 能根据用户代码自动适配。

    「规则定深浅，LLM 定内容」——这是 D4 的核心设计原则。
"""

from __future__ import annotations

import logging
import random
import re

from langchain_core.prompts import ChatPromptTemplate
from langgraph.types import Command

from code_tutor_agent.config import get_llm
from code_tutor_agent.prompts.tutor import (
    DIRECTION_ANALYSIS_SYSTEM,
    DIRECTION_ANALYSIS_USER,
    EMOTION_SIGNAL_KEYWORDS,
    MISCONCEPTION_SYSTEM,
    MISCONCEPTION_USER,
    R01_CODE_LEAK_PATTERNS,
    R10_CODE_WRITE_PATTERNS,
    TUTOR_SYSTEM_PROMPT,
)
from code_tutor_agent.schemas.state import Message, SessionState

logger = logging.getLogger(__name__)

# ── 候选消息池（兜底：LLM 调用失败时的 fallback） ──
_FALLBACK_HINTS = {
    0: "再看看，你的代码还有改进空间 💡",
    1: "思路方向是对的，注意边界条件哦 🤔",
    2: "看看输入为空或只有一个元素的时候会发生什么？",
    3: "第 12 行附近——那个索引在边界处可能会越界 🎯",
    4: "建议：在函数开头加个 `if not nums: return []` 试试",
}

_AC_MESSAGES = [
    "✅ 通过了！代码逻辑正确。",
    "✅ AC！看看能不能优化一下复杂度？",
    "✅ 好样的！下一题继续加油。",
]

_ADVERSARIAL_GENERIC = (
    "✅ 基础用例都通过了！"
    "但在边界或大规模数据下可能会出问题，再检查一下你的实现？"
)

# ── 同类错误连续次数的阈值 ──
_REPEAT_THRESHOLD = 3
_EMOTION_SUBMIT_THRESHOLD = 5


# ═══════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════


def tutor_node(state: SessionState) -> Command:
    logger.info("▶ tutor_node()")
    """L0-L4 辅导决策入口。

    根据 judge 的 verdict + 会话上下文，决定：
    - 给什么级别的提示
    - 要不要插知识点讲解
    - 要不要安抚
    - 后续路由（planner / wait_for_submit）

    Args:
        state: 包含 last_verdict / adversarial_triggered / hint_level / submissions 等。

    Returns:
        Command 路由到下一个节点。
    """
    verdict = state.last_verdict
    hint_level = state.hint_level
    adversarial = state.adversarial_triggered

    # ── Case 1: AC + adversarial fail → 对抗专项反馈 ──
    if verdict == "AC" and adversarial:
        return _handle_adversarial_fail(state)

    # ── Case 2: AC + 全部通过 → 正向反馈 + 评审卡片 ──
    if verdict == "AC":
        return _handle_ac(state)

    # ── Case 3: 基础挂了 → 渐进辅导决策树 ──
    return _handle_base_fail(state, verdict, hint_level)


# ═══════════════════════════════════════════════
#  Mode handlers
# ═══════════════════════════════════════════════


def _handle_adversarial_fail(state: SessionState) -> Command:
    """基础 AC 但对抗挂了 → 给对抗专项反馈。"""
    if state.submissions:
        last_sub = state.submissions[-1]
        adv_results = [
            r for r in last_sub.judge_results
            if r.phase in ("adversarial_boundary", "adversarial_scale")
            and r.status != "AC"
        ]
    else:
        adv_results = []

    if adv_results:
        first = adv_results[0]
        phase_label = "边界测试" if first.phase == "adversarial_boundary" else "规模测试"
        msg = f"✅ 基础用例都通过了！但在 **{phase_label}** 中发现了一个问题：\n{first.detail[:200]}"
    else:
        msg = _ADVERSARIAL_GENERIC

    tutor_msg = Message(role="tutor", content=msg)
    update = {
        "hint_level": max(state.hint_level, 2),
        "tutor_messages": state.tutor_messages + [tutor_msg],
        "status": "awaiting_submit",
    }
    logger.info("Tutor → adversarial_fail")
    logger.debug("Returning Command with goto=%s", 'return Command(update=update, goto="critic_node")')
    return Command(update=update, goto="critic_node")


def _handle_ac(state: SessionState) -> Command:
    """全部通过 → 正向反馈。"""
    msg = random.choice(_AC_MESSAGES)
    review = state.last_review_payload
    if review:
        tc = review.get("time_complexity", "?")
        sc = review.get("space_complexity", "?")
        style = review.get("style_rating", "")
        summary = review.get("summary", "")
        msg += f"\n\n📊 **评审卡片**\n- 时间复杂度: {tc}\n- 空间复杂度: {sc}\n- 风格: {style}\n- {summary}"

    tutor_msg = Message(role="tutor", content=msg)
    update = {
        "tutor_messages": state.tutor_messages + [tutor_msg],
        "status": "done",
    }
    logger.info("Tutor → AC (review=%s)", bool(review))
    logger.debug("Returning Command with goto=%s", 'return Command(update=update, goto="critic_node")')
    return Command(update=update, goto="critic_node")


def _handle_base_fail(
    state: SessionState,
    verdict: str,
    hint_level: int,
) -> Command:
    """基础判题挂了 → L0-L4 决策树。

    步骤：
        1. 收集上下文（提交次数、情绪信号、历史 diff）
        2. 决策树 → 目标 hint_level
        3. LLM 生成提示（含宪法约束）
        4. Post-guard 扫描（防代码泄露）
        5. 返回路由
    """
    submission_count = len(state.submissions)
    emotion_detected = _detect_emotion(state.tutor_messages)
    same_error_count = _count_same_error(state.submissions)

    # ── Step 1: 决策树 ──
    target_level = _decide_hint_level(
        hint_level=hint_level,
        verdict=verdict,
        submission_count=submission_count,
        same_error_count=same_error_count,
        emotion_detected=emotion_detected,
        has_diff=submission_count >= 2,
        state=state,
    )

    # ── Step 2: LLM 生成提示 ──
    hint_text = _generate_hint(state, target_level, verdict)

    # ── Step 3: Post-guard（防代码泄露） ──
    hint_text = _post_guard_scan(hint_text, target_level)

    # ── 构建消息 ──
    tutor_msg = Message(role="tutor", content=hint_text)

    # 决策树可能决定"插讲解" → hint_level 不变，但发一段讲解文本
    # 目前先跟随 target_level 更新
    next_level = min(target_level + 1, 4)

    update = {
        "hint_level": next_level,
        "tutor_messages": state.tutor_messages + [tutor_msg],
        "status": "awaiting_submit",
    }

    logger.info(
        "Tutor → base_fail verdict=%s hint=%d→%d emotion=%s repeat=%d",
        verdict, hint_level, target_level, emotion_detected, same_error_count,
    )
    logger.debug("Returning Command with goto=%s", 'return Command(update=update, goto="critic_node")')
    return Command(update=update, goto="critic_node")


# ═══════════════════════════════════════════════
#  决策树（纯规则，零 LLM）
# ═══════════════════════════════════════════════


def _decide_hint_level(
    hint_level: int,
    verdict: str,
    submission_count: int,
    same_error_count: int,
    emotion_detected: bool,
    has_diff: bool,
    state: SessionState,
) -> int:
    """L0-L4 决策树（PRD §3.3）。

    优先级从高到低：
        1. 同类错误 >= 3 → 插讲解（return 0，但外部会处理）
        2. 提交 >= 5 + 情绪 → L4 + 安抚
        3. 有 diff 且方向对 → 跳级到 min(3, hint+2)
        4. 方向错 + 第 1 次 → L1
        5. 方向错 + 第 2 次 → L2
        6. 默认 → 当前 hint_level

    面试考点：为什么方向检测要用 LLM（_run_direction_analysis）？
        — 规则判断"方向对不对"需要理解代码语义。
          "用户把 for 改成了 while" —— 这是换了个循环方式，不是方向变了。
          "用户加了 if not nums:" —— 这是在修边界，方向对了。
          这种语义判断规则做不了，必须 LLM。
    """
    # 规则 1: 同类错误多次 → 插讲解（标记为 -1，外部处理）
    if same_error_count >= _REPEAT_THRESHOLD:
        logger.info("Decision: same error x%d → concept explanation", same_error_count)
        return max(hint_level, 0)  # keep current level, trigger explanation flag

    # 规则 2: 多次提交 + 情绪 → L4
    if submission_count >= _EMOTION_SUBMIT_THRESHOLD and emotion_detected:
        logger.info("Decision: emotion detected → L4 + comfort")
        return 4

    # 规则 3: 有历史 diff → 分析方向
    if has_diff and len(state.submissions) >= 2:
        direction = _run_direction_analysis(state)
        if direction == "correct":
            target = min(3, hint_level + 2)
            logger.info("Decision: direction correct → jump to L%d", target)
            return target
        elif direction == "wrong":
            # 方向错 → 根据次数定
            if submission_count <= 2:
                return 1
            else:
                return 2

    # 默认
    return hint_level


def _run_direction_analysis(state: SessionState) -> str:
    """LLM 分析：用户这次提交的方向对不对。

    D4 简化版：只在有 >= 2 次提交时才调用。
    """
    if len(state.submissions) < 2:
        return "unknown"

    prev = state.submissions[-2]
    curr = state.submissions[-1]

    llm = get_llm("agnes", temperature=0.1)
    prompt = ChatPromptTemplate.from_messages([
        ("system", DIRECTION_ANALYSIS_SYSTEM),
        ("human", DIRECTION_ANALYSIS_USER),
    ])

    try:
        result = (prompt | llm).invoke({
            "prev_index": prev.index,
            "prev_code": prev.code[:1500],
            "current_index": curr.index,
            "current_code": curr.code[:1500],
        })
        content = result.content if hasattr(result, "content") else str(result)
        if m := re.search(r'"direction":\s*"(\w+)"', content):
            return m.group(1)
    except Exception as exc:
        logger.warning("Direction analysis failed: %s", exc)

    return "unknown"


# ═══════════════════════════════════════════════
#  LLM 提示生成
# ═══════════════════════════════════════════════


def _generate_hint(
    state: SessionState,
    hint_level: int,
    verdict: str,
) -> str:
    """LLM 生成辅导提示。

    嵌入宪法约束（R01/R09/R10）在 system prompt 中。
    如果 LLM 调用失败，回退到模板消息。

    Args:
        state: 当前会话状态。
        hint_level: 目标提示等级（0-4）。
        verdict: 判题结果。

    Returns:
        提示文本（字符串）。
    """
    problem = state.problem
    last_sub = state.submissions[-1] if state.submissions else None

    llm = get_llm("agnes", temperature=0.4)
    prompt = ChatPromptTemplate.from_messages([
        ("system", TUTOR_SYSTEM_PROMPT),
    ])

    # 从 judge results 提取 detail
    judge_detail = ""
    if last_sub and last_sub.judge_results:
        for r in last_sub.judge_results:
            if r.phase == "base":
                judge_detail = r.detail
                break

    try:
        result = (prompt | llm).invoke({
            "hint_level_cap": hint_level,
            "problem_description": problem.description[:800] if problem else "",
            "user_code": last_sub.code[:2000] if last_sub else "",
            "verdict": verdict,
            "judge_detail": judge_detail[:200],
        })
        hint = result.content if hasattr(result, "content") else str(result)
        if hint.strip():
            return hint.strip()
    except Exception as exc:
        logger.warning("LLM hint generation failed: %s", exc)

    # Fallback
    return _FALLBACK_HINTS.get(hint_level, _FALLBACK_HINTS[0])


# ═══════════════════════════════════════════════
#  Post-guard（防 jailbreak）
# ═══════════════════════════════════════════════


def _post_guard_scan(hint: str, target_level: int) -> str:
    """宪法后置守卫：扫描 LLM 输出，防止代码泄露或违规。

    检查项：
        1. R01: 在低等级（<4）下泄露代码 → 降级到 L1 级模糊提示
        2. R10: 代写关键词 → 替换为拒绝话术

    Args:
        hint: LLM 生成的提示文本。
        target_level: 应该遵守的提示等级。

    Returns:
        修正后的提示文本（如果合规则原样返回）。
    """
    # R01 检查：低级提示不能包含代码
    if target_level < 4:
        for pattern in R01_CODE_LEAK_PATTERNS:
            if pattern in hint:
                logger.warning("POST-GUARD: R01 violation detected (pattern=%s) — downgrading", pattern)
                return _FALLBACK_HINTS.get(max(target_level - 1, 0), _FALLBACK_HINTS[0])

    # R10 检查：不能代写
    for pattern in R10_CODE_WRITE_PATTERNS:
        if pattern in hint:
            logger.warning("POST-GUARD: R10 violation detected (pattern=%s) — replacing", pattern)
            return "我可以陪你梳理思路，但手得你自己动～先试着改改，看看哪里可能出问题？"

    return hint


# ═══════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════


def _detect_emotion(messages: list[Message]) -> bool:
    """从用户最近的几条消息中检测情绪信号（纯关键词，零 LLM）。

    面试考点：为什么情绪检测不用 LLM？
        — 关键词命中率足够（"烦"、"放弃了"），且可以 0 latency。
          如果用户说了"我有点烦，但还能坚持"，关键词命中但不需要降级，
          这个误报在 MVP 阶段可以接受。
    """
    for msg in messages[-3:]:  # 只看最近 3 条
        if msg.role == "user":
            for kw in EMOTION_SIGNAL_KEYWORDS:
                if kw in msg.content:
                    return True
    return False


def _count_same_error(submissions: list) -> int:
    """统计同一题的同一类错误连续出现了几次。

    策略：看最后几个 judge_results，如果都是 WA 且 detail 相似 → 同类错误。
    D4 简化版：只看 last_verdict 是否连续相同。
    """
    recent_verdicts = []
    for sub in submissions[-5:]:  # 只看最近 5 次
        for r in sub.judge_results:
            if r.phase == "base":
                recent_verdicts.append(r.status)
                break

    if not recent_verdicts:
        return 0

    # 从尾部向前数连续相同 verdict
    last_v = recent_verdicts[-1]
    count = 1
    for v in reversed(recent_verdicts[:-1]):
        if v == last_v:
            count += 1
        else:
            break
    return count