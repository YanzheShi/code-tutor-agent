"""Tests for the Agent Tutor node — 判题非 AC 后的辅导文案生成。

架构变更：
    2026-10-13 agent-only 重构：不再 return Command(goto)，改为返回纯 dict
    ``{"status": "awaiting_submit"}``；图通过静态边
    ``agent_tutor_node → wait_for_submit_node`` 路由（确定性单出口，改静态边
    可避免与 Command 并存导致的双执行历史坑，见 2026-08-04）。

    2026-10-25 职责归位：本节点成为**非 AC 辅导文案的唯一宿主**——
    判题只写 last_exec_results 交棒客观执行结果，文案（warm_feedback /
    repair_suggestion / tutor_messages）在这里生成。verdict 权威仍属于判题，
    本节点只把它当 forced_verdict 锁死 LLM。

    AC 线不经过本节点（由 agent_judge_router 直接路由到 update_profile_node /
    wait_for_submit_node），故 AC 的祝贺文案仍由判题节点生成，本测试不涉及。
"""

from __future__ import annotations

from unittest.mock import patch

from code_tutor_agent.agents.agent_judge import JudgeAnalysis
from code_tutor_agent.nodes.agent_tutor import agent_tutor_node
from code_tutor_agent.schemas.state import ProblemMeta, SessionState, Submission

_EXEC_RESULTS = [
    {
        "test_case_id": 0,
        "status": "Wrong Answer",
        "detail": "expected=[0,1] got=[0,0]",
        "runtime_ms": 5.0,
        "memory_kb": 1024.0,
        "expected_output": "[0,1]",
        "actual_output": "[0,0]",
    },
    {
        "test_case_id": 1,
        "status": "Passed",
        "detail": "",
        "runtime_ms": 3.0,
        "memory_kb": 512.0,
        "expected_output": "[1,2]",
        "actual_output": "[1,2]",
    },
]


def _make_state(
    verdict: str = "WA",
    cycle: int = 1,
    scope: str = "full",
    exec_results: list | None = None,
) -> SessionState:
    """Helper: build a state as agent_judge_node would leave it after a non-AC cycle."""
    return SessionState(
        session_id="test-tutor",
        mode="agent",
        status="tutoring",
        last_verdict=verdict or None,
        judge_cycle=cycle,
        judge_scope=scope,
        topic="数组",
        difficulty="easy",
        problem=ProblemMeta(
            problem_id=1,
            title="测试题",
            topic="数组",
            difficulty="easy",
            description="测试描述",
        ),
        submissions=[Submission(index=1, code="class Solution:\n    pass", timestamp="")],
        last_exec_results=exec_results if exec_results is not None else _EXEC_RESULTS,
    )


def _last_tutor_text(result: dict) -> str:
    msgs = result["tutor_messages"]
    return str(msgs[-1].content if hasattr(msgs[-1], "content") else msgs[-1]["content"])


class TestAgentTutorFullScope:
    """提交（full）非 AC：调 LLM 生成针对性指导。"""

    def test_writes_feedback_and_appends_tutor_message(self):
        state = _make_state(verdict="WA")

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            mock_analyze.return_value = JudgeAnalysis(
                verdict="WA",
                warm_feedback="大部分对了，继续加油！",
                repair_suggestion="检查双指针的移动逻辑",
                should_retry=True,
            )

            result = agent_tutor_node(state)

        assert isinstance(result, dict) and "goto" not in result
        assert result["status"] == "awaiting_submit"
        assert result["warm_feedback"] == "大部分对了，继续加油！"
        assert result["repair_suggestion"] == "检查双指针的移动逻辑"
        assert "检查双指针" in _last_tutor_text(result)
        assert "**修复建议**" in _last_tutor_text(result)

    def test_forced_verdict_locks_the_llm(self):
        """verdict 权威属于判题：必须作为 forced_verdict 传给 LLM，且本节点不改判。"""
        state = _make_state(verdict="TLE")

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            mock_analyze.return_value = JudgeAnalysis(
                verdict="AC", warm_feedback="乱判的", repair_suggestion="", should_retry=False,
            )
            result = agent_tutor_node(state)

            assert mock_analyze.call_args.kwargs["forced_verdict"] == "TLE"
            # 交棒载荷被还原成逐用例结果（含期望/实际输出，防 LLM 臆造）
            passed_results = mock_analyze.call_args.kwargs["results"]
            assert passed_results[0].expected_output == "[0,1]"
            assert passed_results[0].actual_output == "[0,0]"

        assert "last_verdict" not in result

    def test_llm_failure_degrades_to_deterministic_text(self):
        """LLM 彻底不可用只降文案质量，绝不击穿 invoke（否则会话失去挂起节点）。"""
        state = _make_state(verdict="RE")

        with patch(
            "code_tutor_agent.nodes.agent_tutor.analyze_judge_results",
            side_effect=RuntimeError("gateway down"),
        ):
            result = agent_tutor_node(state)

        assert result["status"] == "awaiting_submit"
        assert result["warm_feedback"]
        assert result["tutor_messages"]

    def test_missing_handoff_payload_skips_llm(self):
        """交棒载荷为空（老会话/异常）：不调 LLM，给确定性重试提示。"""
        state = _make_state(verdict="WA", exec_results=[])

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            result = agent_tutor_node(state)

        mock_analyze.assert_not_called()
        assert "重新提交" in _last_tutor_text(result)
        assert result["status"] == "awaiting_submit"


class TestAgentTutorSampleScope:
    """运行（sample）非 AC：绝不为一句反馈等 LLM。"""

    def test_uses_local_counts_and_never_calls_llm(self):
        state = _make_state(verdict="WA", scope="sample")

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            result = agent_tutor_node(state)

        mock_analyze.assert_not_called()
        assert "样例用例 1/2" in result["warm_feedback"]
        assert result["repair_suggestion"] == ""
        assert result["status"] == "awaiting_submit"


class TestAgentTutorStateHygiene:
    """节点出口的 state 纪律。"""

    def test_does_not_touch_judge_cycle(self):
        """judge_cycle 由判题节点 +1，本节点不得回写。"""
        state = _make_state(verdict="WA", cycle=5)

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            mock_analyze.return_value = JudgeAnalysis(
                verdict="WA", warm_feedback="继续加油", repair_suggestion="", should_retry=True,
            )
            result = agent_tutor_node(state)

        assert "judge_cycle" not in result

    def test_appends_to_existing_transcript(self):
        """追加而非覆盖：历史 tutor_messages 必须原样保留。"""
        state = _make_state(verdict="WA")
        state.tutor_messages = []
        from code_tutor_agent.schemas.state import Message
        state.tutor_messages = [Message(role="user", content="我哪里错了？")]

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            mock_analyze.return_value = JudgeAnalysis(
                verdict="WA", warm_feedback="看第二个用例", repair_suggestion="", should_retry=True,
            )
            result = agent_tutor_node(state)

        assert len(result["tutor_messages"]) == 2
        assert result["tutor_messages"][0].content == "我哪里错了？"
