"""Tests for the Agent Tutor node — post-judge routing.

架构变更（agent-only 重构，2026-08-13）：
    agent_tutor_node 不再 return Command(goto)，改为返回纯 dict
    ``{"status": "awaiting_submit"}``；图通过静态边
    ``agent_tutor_node → wait_for_submit_node`` 路由（确定性单出口，改静态边
    可避免与 Command 并存导致的双执行历史坑，见 2026-08-04）。

    因此本节点**不再按 verdict 分支**（AC 不走此节点 —— 由 agent_judge_router
    直接路由到 update_profile_node / wait_for_submit_node）。本测试只验证：
    1. 返回纯 dict（非 Command）
    2. 任意 verdict 都返回 status=awaiting_submit
    3. 只写 status 一个键，不污染其它 state 字段
    4. judge_cycle 不被改动
"""

from __future__ import annotations

from code_tutor_agent.nodes.agent_tutor import agent_tutor_node
from code_tutor_agent.schemas.state import SessionState


def _make_state(verdict: str = "", cycle: int = 1) -> SessionState:
    """Helper: build a minimal state with judge results."""
    return SessionState(
        session_id="test-tutor",
        mode="agent",
        status="tutoring",
        last_verdict=verdict or None,
        judge_cycle=cycle,
        warm_feedback="反馈消息",
        repair_suggestion="修复建议",
    )


class TestAgentTutorNode:
    """Verify the node returns a plain dict awaiting_submit for any verdict."""

    def test_returns_plain_dict_not_command(self):
        """节点应返回纯 dict，而非 langgraph Command。"""
        state = _make_state(verdict="WA", cycle=1)
        result = agent_tutor_node(state)

        assert isinstance(result, dict), f"应返回 dict，实际: {result!r}"
        assert "goto" not in result, "不应再含 goto（已由静态边路由）"

    def test_returns_awaiting_submit_for_wa(self):
        """WA → status=awaiting_submit（经静态边回 wait_for_submit）。"""
        state = _make_state(verdict="WA", cycle=1)
        result = agent_tutor_node(state)
        assert result["status"] == "awaiting_submit"

    def test_returns_awaiting_submit_for_re(self):
        """RE → 同样回 awaiting_submit。"""
        state = _make_state(verdict="RE", cycle=3)
        result = agent_tutor_node(state)
        assert result["status"] == "awaiting_submit"

    def test_returns_awaiting_submit_for_tle(self):
        """TLE → 同样回 awaiting_submit。"""
        state = _make_state(verdict="TLE", cycle=1)
        result = agent_tutor_node(state)
        assert result["status"] == "awaiting_submit"

    def test_returns_awaiting_submit_for_empty_verdict(self):
        """空 verdict（兜底）→ 同样回 awaiting_submit。"""
        state = _make_state(verdict="", cycle=1)
        result = agent_tutor_node(state)
        assert result["status"] == "awaiting_submit"

    def test_only_writes_status_key(self):
        """节点只写 status，不应覆盖 warm_feedback / repair_suggestion 等字段。

        （LangGraph 运行时未显式返回的键会被保留，不会清掉。）
        """
        state = _make_state(verdict="WA", cycle=2)
        result = agent_tutor_node(state)

        assert result == {"status": "awaiting_submit"}, (
            f"节点只应返回 {{status: awaiting_submit}}，实际: {result!r}"
        )

    def test_judge_cycle_preserved(self):
        """节点不改 judge_cycle（由 agent_judge_node 负责 +1）。"""
        state = _make_state(verdict="WA", cycle=5)
        result = agent_tutor_node(state)
        assert "judge_cycle" not in result
