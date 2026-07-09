"""Tests for the Agent Judge node — LangGraph node behavior.

Coverage:
    1. No submission → error state
    2. No problem_id → error state
    3. Successful AC judging path
    4. Failed WA judging path
    5. State updates (verdict, warm_feedback, judge_cycle)
"""

from __future__ import annotations

from unittest.mock import patch

from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.sandbox.runner import RunnerResult
from code_tutor_agent.schemas.state import JudgeResult, ProblemMeta, SessionState, Submission


def _make_state(
    session_id: str = "test-judge",
    verdict: str = "",
    code: str = "class Solution:\n    def solve(self):\n        return 42",
) -> SessionState:
    """Helper: build a minimal SessionState with one submission."""
    return SessionState(
        session_id=session_id,
        mode="agent",
        status="awaiting_submit",
        problem=ProblemMeta(
            problem_id=1,
            title="测试题",
            topic="数组",
            difficulty="easy",
            description="测试",
            starter_code="class Solution:\n    def solve(self):\n        pass",
        ),
        submissions=[
            Submission(
                index=1,
                code=code,
                verdict=verdict,
                timestamp="2026-07-08T12:00:00",
            ),
        ],
    )


class TestAgentJudgeNode:
    """Verify the LangGraph node's behavior under various conditions."""

    def test_no_submission_returns_error(self):
        """Without submissions, should route to error."""
        state = SessionState(
            session_id="test-empty",
            mode="agent",
            status="awaiting_submit",
        )
        result = agent_judge_node(state)
        assert result.goto == "__end__"
        assert result.update.get("status") == "error"

    def test_no_problem_returns_error(self):
        """Without a problem, should route to error."""
        state = SessionState(
            session_id="test-no-problem",
            mode="agent",
            status="awaiting_submit",
            submissions=[
                Submission(index=1, code="print(1)", verdict="", timestamp=""),
            ],
        )
        result = agent_judge_node(state)
        assert result.goto == "__end__"
        assert result.update.get("status") == "error"

    def test_ac_path(self):
        """All tests pass → verdict=AC, should_retry=False."""
        state = _make_state()

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            # Mock DB returns
            mock_db.return_value = {
                "title": "测试题",
                "topic": "数组",
                "difficulty": "easy",
                "description": "测试描述",
                "test_cases": [
                    {"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"},
                    {"input_args": ["[3,2,4]", "6"], "expected_output": "[1,2]"},
                ],
            }
            # Mock Judge0 results
            mock_run.return_value = [
                RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
                RunnerResult(1, "Passed", "[1,2]", runtime_ms=3.0),
            ]
            # Mock LLM analysis
            from code_tutor_agent.agents.agent_judge import JudgeAnalysis
            mock_analyze.return_value = JudgeAnalysis(
                verdict="AC",
                warm_feedback="恭喜！全部通过 🎉",
                repair_suggestion="可以尝试 O(1) 空间解法",
                should_retry=False,
            )

            result = agent_judge_node(state)

            assert result.goto == "agent_tutor_node"
            assert result.update["last_verdict"] == "AC"
            assert result.update["judge_cycle"] == 1
            assert "恭喜" in result.update["warm_feedback"]

    def test_wa_path(self):
        """Some tests fail → verdict=WA, should_retry=True."""
        state = _make_state()

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            mock_db.return_value = {
                "title": "测试题",
                "topic": "数组",
                "difficulty": "easy",
                "description": "测试描述",
                "test_cases": [
                    {"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"},
                ],
            }
            mock_run.return_value = [
                RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[0,0]", runtime_ms=5.0),
            ]
            from code_tutor_agent.agents.agent_judge import JudgeAnalysis
            mock_analyze.return_value = JudgeAnalysis(
                verdict="WA",
                warm_feedback="大部分对了，继续加油！",
                repair_suggestion="检查双指针的移动逻辑",
                should_retry=True,
            )

            result = agent_judge_node(state)

            assert result.goto == "agent_tutor_node"
            assert result.update["last_verdict"] == "WA"
            assert result.update["judge_cycle"] == 1
            # should_retry is inside JudgeAnalysis, not in state update
            # tutor_messages should contain the feedback
            msgs = result.update.get("tutor_messages", [])
            assert any("检查双指针" in str(m) for m in msgs)

    def test_judge_cycle_increments(self):
        """judge_cycle should increment on each judge call."""
        state = _make_state()
        state.judge_cycle = 2  # third submission

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            mock_db.return_value = {
                "title": "测试题",
                "topic": "数组",
                "difficulty": "easy",
                "description": "测试描述",
                "test_cases": [{"input_args": ["[1]", "1"], "expected_output": "1"}],
            }
            mock_run.return_value = [
                RunnerResult(0, "Passed", "1", runtime_ms=1.0),
            ]
            from code_tutor_agent.agents.agent_judge import JudgeAnalysis
            mock_analyze.return_value = JudgeAnalysis(
                verdict="AC", warm_feedback="过了！", repair_suggestion="",
                should_retry=False,
            )

            result = agent_judge_node(state)
            assert result.update["judge_cycle"] == 3  # 2 + 1