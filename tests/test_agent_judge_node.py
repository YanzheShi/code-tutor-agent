"""Tests for the Agent Judge node — LangGraph node behavior + router.

架构变更（agent-only 重构，2026-08-13）：
    agent_judge_node 不再 return Command(goto)，改为返回纯 dict；
    路由由图的条件边 ``agent_judge_router`` 决定：
      - status=="error"                              → END
      - WA/RE/TLE/CE（last_verdict != "AC")          → agent_tutor_node
      - AC 且（运行 is_run 或 scope=sample）         → wait_for_submit_node（不写画像、不 done）
      - AC 且 真实提交（full + 非运行）              → update_profile_node（写 v2 画像 → critic）

    运行/提交统一收口：scope 由 wait_for_submit_node 注入 state.judge_scope。
    运行（sample）只写诊断 last_run_results，不污染学习者画像（微决策 1，解读 X）。
"""

from __future__ import annotations

from unittest.mock import patch

from langgraph.graph import END as END_REF

from code_tutor_agent.db.models import DBProblem
from code_tutor_agent.graph.graph import agent_judge_router
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.sandbox.runner import RunnerResult
from code_tutor_agent.schemas.state import (
    JudgeResult,
    ProblemMeta,
    SessionState,
    Submission,
)


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
        """Without submissions, should set status=error (router → END)."""
        state = SessionState(
            session_id="test-empty",
            mode="agent",
            status="awaiting_submit",
        )
        result = agent_judge_node(state)
        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert "goto" not in result

    def test_no_problem_returns_error(self):
        """Without a problem, should set status=error (router → END)."""
        state = SessionState(
            session_id="test-no-problem",
            mode="agent",
            status="awaiting_submit",
            submissions=[
                Submission(index=1, code="print(1)", verdict="", timestamp=""),
            ],
        )
        result = agent_judge_node(state)
        assert isinstance(result, dict)
        assert result["status"] == "error"

    def test_ac_path(self):
        """All tests pass → verdict=AC, full scope → status=done + profile_delta."""
        state = _make_state()

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            # Mock DB returns
            mock_db.return_value = DBProblem(
                id=1,
                title="测试题",
                topic="数组",
                difficulty="easy",
                description="测试描述",
                test_cases_json='[{"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"}, {"input_args": ["[3,2,4]", "6"], "expected_output": "[1,2]"}]',
            )
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

            # 返回纯 dict（非 Command）；AC 全量 → status=done + 写画像由 router 路由。
            assert isinstance(result, dict)
            assert "goto" not in result
            assert result["last_verdict"] == "AC"
            assert result["judge_cycle"] == 1
            assert result["status"] == "done"
            assert result["profile_delta"]["outcome"] == "AC"
            assert "恭喜" in result["warm_feedback"]

    def test_wa_path(self):
        """Some tests fail → verdict=WA, status=tutoring (router → agent_tutor)."""
        state = _make_state()

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            mock_db.return_value = DBProblem(
                id=1,
                title="测试题",
                topic="数组",
                difficulty="easy",
                description="测试描述",
                test_cases_json='[{"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"}]',
            )
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

            assert isinstance(result, dict)
            assert result["last_verdict"] == "WA"
            assert result["judge_cycle"] == 1
            assert result["status"] == "tutoring"
            # should_retry is inside JudgeAnalysis; repair suggestion flows into tutor_messages
            msgs = result.get("tutor_messages", [])
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
            mock_db.return_value = DBProblem(
                id=1,
                title="测试题",
                topic="数组",
                difficulty="easy",
                description="测试描述",
                test_cases_json='[{"input_args": ["[1]", "1"], "expected_output": "1"}]',
            )
            mock_run.return_value = [
                RunnerResult(0, "Passed", "1", runtime_ms=1.0),
            ]
            from code_tutor_agent.agents.agent_judge import JudgeAnalysis
            mock_analyze.return_value = JudgeAnalysis(
                verdict="AC", warm_feedback="过了！", repair_suggestion="",
                should_retry=False,
            )

            result = agent_judge_node(state)
            assert result["judge_cycle"] == 3  # 2 + 1

    def test_sample_scope_run_writes_diagnostic_no_profile(self):
        """运行（scope=sample）只写诊断 last_run_results，不写画像、不 done。

        对应 运行 按钮：agent_judge_node 用 visible_test_cases，写 last_run_results，
        但绝不写 profile_delta（微决策 1，解读 X），status 保持 awaiting_submit，
        由 router 路由回 wait_for_submit_node 维持循环。
        """
        state = _make_state()
        state.judge_scope = "sample"  # 运行按钮注入

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            mock_db.return_value = DBProblem(
                id=1,
                title="测试题",
                topic="数组",
                difficulty="easy",
                description="测试描述",
                visible_test_cases_json='[{"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"}]',
            )
            mock_run.return_value = [
                RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
            ]
            from code_tutor_agent.agents.agent_judge import JudgeAnalysis
            mock_analyze.return_value = JudgeAnalysis(
                verdict="AC", warm_feedback="样例都过了", repair_suggestion="",
                should_retry=False,
            )

            result = agent_judge_node(state)

            # 运行（sample）绝对不调 LLM 判题分析（方案 A：运行=快速自测）
            mock_analyze.assert_not_called()
            # 不 done、不写画像
            assert result["status"] == "awaiting_submit"
            assert "profile_delta" not in result
            # 写诊断
            assert "last_run_results" in result
            assert result["last_run_results"][0]["passed"] is True
            # 反馈来自本地构造（"样例用例 ... 通过 ✅"），而非 LLM 文案
            assert "样例用例" in result["warm_feedback"]
            # 微决策 3：样例全过 → 鼓励提交完整用例
            assert "💡" in result["warm_feedback"]

    def test_sample_scope_skips_llm_wa_uses_local_feedback(self):
        """运行（sample）+ WA：同样跳过 LLM，本地 verdict 来自执行引擎客观结果。

        证明方案 A 对 WA 也生效——运行遇到失败用例不会再等 LLM 推理。
        """
        state = _make_state()
        state.judge_scope = "sample"

        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
        ):
            mock_db.return_value = DBProblem(
                id=1,
                title="测试题",
                topic="数组",
                difficulty="easy",
                description="测试描述",
                visible_test_cases_json='[{"input_args": ["[1,2,3]", "5"], "expected_output": "[0,1]"}]',
            )
            mock_run.return_value = [
                RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[0,0]", runtime_ms=5.0),
            ]

            result = agent_judge_node(state)

            # LLM 必须被跳过
            mock_analyze.assert_not_called()
            # verdict 来自执行引擎（WA），本地反馈
            assert result["last_verdict"] == "WA"
            assert "样例用例" in result["warm_feedback"]
            # 非 AC → 路由到 agent_tutor_node
            assert result["status"] == "tutoring"


class TestAgentJudgeRouter:
    """Verify agent_judge_router routes by (status, verdict, scope, is_run)."""

    def _state(self, *, status="judging", verdict=None, scope="full", is_run=False) -> SessionState:
        return SessionState(
            session_id="router-test",
            mode="agent",
            status=status,
            last_verdict=verdict,
            judge_scope=scope,
            submissions=[Submission(index=1, code="x", is_run=is_run)],
        )

    def test_error_status_routes_to_end(self):
        state = self._state(status="error", verdict=None)
        assert agent_judge_router(state) is END_REF

    def test_wa_routes_to_agent_tutor(self):
        state = self._state(verdict="WA")
        assert agent_judge_router(state) == "agent_tutor_node"

    def test_re_routes_to_agent_tutor(self):
        state = self._state(verdict="RE")
        assert agent_judge_router(state) == "agent_tutor_node"

    def test_full_ac_routes_to_update_profile(self):
        state = self._state(verdict="AC", scope="full", is_run=False)
        assert agent_judge_router(state) == "update_profile_node"

    def test_sample_ac_routes_to_wait_for_submit(self):
        state = self._state(verdict="AC", scope="sample", is_run=False)
        assert agent_judge_router(state) == "wait_for_submit_node"

    def test_run_is_run_ac_routes_to_wait_for_submit(self):
        state = self._state(verdict="AC", scope="full", is_run=True)
        assert agent_judge_router(state) == "wait_for_submit_node"
