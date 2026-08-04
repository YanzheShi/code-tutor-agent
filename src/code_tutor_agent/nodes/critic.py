"""critic_node — 宪法约束独立评审 + flush 当前题 → problem_history。

流程：
    tutor_node 产出提示草稿 → critic_node
      1. flush 当前题 → ProblemAttemptRecord → push problem_history
      2. 宪法评审（R01 / R04）
      3. 路由：AC / ABANDON → planner_node（下一题），WA → wait_for_submit_node（继续改）

评审规则：
    - R01: 低等级（<4）下不能泄露完整代码
    - R04: 检测用户挫败情绪
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from langgraph.types import Command

from code_tutor_agent.memory import schedule_extraction
from code_tutor_agent.schemas.state import (
    ProblemAttemptRecord,
    SessionPhase,
    SessionState,
)

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


def _resolve_verdict(state: SessionState) -> str:
    """Determine the terminal verdict for the current problem."""
    if state.pending_abandon:
        return "ABANDON"
    return state.last_verdict or "WA"


def _build_record(state: SessionState, verdict: str) -> ProblemAttemptRecord:
    """Build a ProblemAttemptRecord from current session state."""
    last_code = state.submissions[-1].code if state.submissions else ""
    tags = [state.problem.tag_primary] if state.problem else []
    return ProblemAttemptRecord(
        problem_id=state.problem.problem_id if state.problem else 0,
        title=state.problem.title if state.problem else "",
        tags=tags,
        difficulty=state.problem.difficulty if state.problem else "",
        verdict=verdict,
        user_code_final=last_code,
        hint_level_reached=state.hint_level,
        tutor_messages_count=len(state.tutor_messages),
        diagnosis=state.last_diagnosis,
        abandoned=(verdict == "ABANDON"),
    )


def _maybe_extract(state: SessionState) -> None:
    """Episode 终结 → 异步语义记忆抽取(见 docs/agent-memory-design.md)。

    只在「新 episode 真正终结」的非去重分支调用;调度本身失败也绝不影响主流程。
    """
    try:
        schedule_extraction(state)
    except Exception as exc:
        logger.warning("memory extraction scheduling failed (non-fatal): %s", exc)


def critic_node(state: SessionState) -> Command[Literal["wait_for_submit_node", "planner_node"]]:
    """Flush current problem → history, constitutional review, then route.

    - AC / ABANDON → planner_node（下一题）
    - WA           → wait_for_submit_node（继续改）
    """
    logger.info("▶ critic_node() — last_verdict=%s pending_abandon=%s", state.last_verdict, state.pending_abandon)

    # ── 1. resolve verdict & flush ──
    verdict = _resolve_verdict(state)
    record = _build_record(state, verdict)
    new_history = list(state.problem_history) + [record]

    # ── 2. R01 检查 ──
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

    # ── 3. R04 情绪检测 ──
    user_msgs = [m for m in state.tutor_messages if m.role == "user"]
    if user_msgs:
        last_user = user_msgs[-1].content
        if any(kw in last_user for kw in R04_FRUSTRATION_KEYWORDS):
            logger.info("R04: user frustration detected")

    # ── 4. 通用 update（换题清理） ──
    # 注意：next_preference 不在此处清——ABANDON 分支 goto planner_node，
    # planner 需要读取它选下一题（由 planner 消费后自行置 None）。
    updates = {
        "problem_history": new_history,
        "total_problems": state.total_problems + 1,
        "problem": None,
        "tutor_messages": [],
        "hint_level": 0,
        "last_diagnosis": None,
        "pending_abandon": False,
        "phase": SessionPhase.done,
    }

    # ── 5. 路由 ──
    # AC → phase=reviewing, 不清 problem/tutor_messages, 去 END（前端显示「下一题」按钮）
    # ABANDON (/next-problem) → 清所有, 去 planner_node 生成新题
    # WA → 清所有, 去 wait_for_submit_node 等重提交
    if verdict == "ABANDON":
        logger.info("critic_node → goto=planner_node (verdict=%s)", verdict)
        # 若该题目此前已 flush 过（典型场景：已 AC 后点「下一题」，
        # 会经 /next-problem 以 pending_abandon 重入本节点），
        # 则不再重复追加一条 ABANDON 记录，仅清题进入下一题，
        # 避免同一题在 problem_history 出现两条记录。
        last_rec = state.problem_history[-1] if state.problem_history else None
        cur_pid = state.problem.problem_id if state.problem else 0
        if last_rec is not None and last_rec.problem_id == cur_pid and last_rec.verdict in ("AC", "WA"):
            return Command(goto="planner_node", update={
                "problem": None,
                "tutor_messages": [],
                "hint_level": 0,
                "last_diagnosis": None,
                "pending_abandon": False,
                "phase": SessionPhase.done,
            })
        _maybe_extract(state)  # 新 episode 终结(ABANDON)→ 异步记忆抽取
        return Command(goto="planner_node", update=updates)
    elif verdict == "AC":
        logger.info("critic_node → goto=wait_for_submit_node (verdict=AC, 重新暂停等待重提交)")
        # AC 后不清 problem/tutor_messages（前端需展示 AC 消息），
        # 并重新暂停在 wait_for_submit_node（interrupt），使得用户「继续提交不同解法」
        # 时 submit 端点可通过 Command(resume) 正常续跑判题，无需终止 graph。
        # 去重：若末条 problem_history 已为同一题且 verdict 相同（重复 AC 重提交），
        # 不再追加重复记录。
        cur_pid = state.problem.problem_id if state.problem else 0
        last_rec = state.problem_history[-1] if state.problem_history else None
        if (last_rec is not None
                and last_rec.problem_id == cur_pid
                and last_rec.verdict == verdict):
            ac_update = {
                "phase": "reviewing",
                "pending_abandon": False,
                "next_preference": None,
            }
        else:
            ac_update = {
                "problem_history": new_history,
                "total_problems": state.total_problems + 1,
                "phase": "reviewing",
                "pending_abandon": False,
                "next_preference": None,
            }
            _maybe_extract(state)  # 新 episode 终结(AC)→ 异步记忆抽取
        return Command(goto="wait_for_submit_node", update=ac_update)
    else:
        logger.info("critic_node → goto=wait_for_submit_node (verdict=%s)", verdict)
        return Command(goto="wait_for_submit_node", update=updates)