"""Agent 判题节点 — 通过 Judge0 进行 LLM 驱动的判题 LangGraph 节点。

该节点在 agent 模式下替代传统的机械判题节点 judge_node。
它不直接比较期望输出与实际输出，而是：
  1. 通过 Judge0 对测试用例执行用户代码
  2. 将原始结果传给 LLM 进行解读

节点流转：
    wait_for_submit_node → agent_judge_node → agent_tutor_node
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

from code_tutor_agent.agents.agent_judge import analyze_judge_results
from code_tutor_agent.db.database import get_problem_by_id
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# Timeout per test case during agent judging
AGENT_JUDGE_TIMEOUT = 5.0


def _last_submission(state: SessionState) -> dict[str, Any] | None:
    """Get the most recent submission from state, or None."""
    if not state.submissions:
        return None
    last = state.submissions[-1]
    return {"code": last.code, "index": last.index}


def agent_judge_node(state: SessionState) -> Command:
    """LangGraph node: LLM-driven judging via Judge0.

    1. Loads the problem from DB to get test cases
    2. Runs the user's last submission against test cases
    3. Passes raw results to LLM for analysis + warm feedback
    4. Stores verdict, feedback, and suggestion in state
    5. Routes to agent_tutor_node

    Args:
        state: Session state with submissions and problem info.

    Returns:
        Command routing to agent_tutor_node.
    """
    logger.info("▶ agent_judge_node() — cycle=%d", state.judge_cycle + 1)

    # ── Get the latest submission ──
    sub = _last_submission(state)
    if not sub:
        logger.warning("No submission found — routing to error")
        return Command(
            update={"status": "error", "error_message": "No submission to judge"},
            goto="__end__",
        )

    code = sub["code"]
    problem_id = state.problem.problem_id if state.problem else 0
    if not problem_id:
        logger.warning("No problem_id in state — routing to error")
        return Command(
            update={"status": "error", "error_message": "No problem loaded"},
            goto="__end__",
        )

    # ── Load problem from DB ──
    problem_dict = get_problem_by_id(problem_id)
    if not problem_dict:
        logger.warning("Problem %d not found in DB", problem_id)
        return Command(
            update={"status": "error", "error_message": f"Problem {problem_id} not found"},
            goto="__end__",
        )

    test_cases = problem_dict.get("test_cases", [])
    if not test_cases:
        logger.warning("No test cases for problem %d", problem_id)
        return Command(
            update={"status": "error", "error_message": "No test cases available"},
            goto="__end__",
        )

    logger.info("Running %d test cases via Judge0...", len(test_cases))

    # ── Run test cases via Judge0 (or local fallback) ──
    raw_results = run_solution(code, test_cases, timeout=AGENT_JUDGE_TIMEOUT)
    logger.info("Judge0 returned %d results", len(raw_results))

    # ── LLM analysis of raw results ──
    description = problem_dict.get("description", "")
    analysis = analyze_judge_results(
        code=code,
        title=problem_dict.get("title", ""),
        difficulty=problem_dict.get("difficulty", state.difficulty),
        topic=problem_dict.get("topic", state.topic),
        description=description,
        results=raw_results,
    )

    logger.info("Verdict: %s | should_retry=%s", analysis.verdict, analysis.should_retry)

    # ── Build the tutor message with warm feedback ──
    feedback_msg = analysis.warm_feedback
    if analysis.repair_suggestion:
        feedback_msg += f"\n\n**修复建议**\n{analysis.repair_suggestion}"

    # ── Update state ──
    update = {
        "last_verdict": analysis.verdict,
        "warm_feedback": analysis.warm_feedback,
        "repair_suggestion": analysis.repair_suggestion,
        "judge_cycle": state.judge_cycle + 1,
        "tutor_messages": state.tutor_messages
        + [{"role": "tutor", "content": feedback_msg}],
    }

    # ── Route based on verdict ──
    if analysis.verdict == "AC":
        logger.info("AC — routing to agent_tutor_node (done)")
        update["status"] = "tutoring"
        return Command(update=update, goto="agent_tutor_node")
    else:
        logger.info("Not AC — routing to agent_tutor_node for retry guidance")
        update["status"] = "tutoring"
        return Command(update=update, goto="agent_tutor_node")