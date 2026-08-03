"""Tests for the Agent Tutor node — routing after LLM-driven judging.

Coverage:
    1. AC verdict → status=done, phase=reviewing, goto=__end__（兜底分支，正常不可达）
    2. WA verdict → status=awaiting_submit, goto=wait_for_submit_node
    3. RE/TLE verdict → same as WA (loop back)
    4. Empty verdict → loop back (safe default)
    5. judge_cycle preserved across routing
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
    """Verify routing decisions based on verdict."""

    def test_ac_branch_defensive_ends(self):
        """AC 分支：正常流程不可达（AC 收尾由 agent_judge → update_profile →
        critic 的 AC 分支完成，原静态边双执行冲突已于 2026-08-04 修复），
        仅作兜底：status=done, phase=reviewing, goto=__end__。"""
        state = _make_state(verdict="AC", cycle=2)
        result = agent_tutor_node(state)

        assert result.goto == "__end__"
        assert result.update["status"] == "done"
        assert result.update["phase"] == "reviewing"

    def test_wa_routes_to_wait_for_submit(self):
        """WA → status=awaiting_submit, goto=wait_for_submit_node."""
        state = _make_state(verdict="WA", cycle=1)
        result = agent_tutor_node(state)

        assert result.goto == "wait_for_submit_node"
        assert result.update["status"] == "awaiting_submit"

    def test_re_routes_to_wait_for_submit(self):
        """RE → same as WA, loop back."""
        state = _make_state(verdict="RE", cycle=3)
        result = agent_tutor_node(state)

        assert result.goto == "wait_for_submit_node"
        assert result.update["status"] == "awaiting_submit"

    def test_tle_routes_to_wait_for_submit(self):
        """TLE → same as WA, loop back."""
        state = _make_state(verdict="TLE", cycle=1)
        result = agent_tutor_node(state)

        assert result.goto == "wait_for_submit_node"
        assert result.update["status"] == "awaiting_submit"

    def test_empty_verdict_loops_back(self):
        """Empty verdict (shouldn't happen) → safe default: loop back."""
        state = _make_state(verdict="", cycle=1)
        result = agent_tutor_node(state)

        assert result.goto == "wait_for_submit_node"

    def test_preserves_state_fields(self):
        """The node should not overwrite warm_feedback/repair_suggestion."""
        state = _make_state(verdict="AC", cycle=2)
        result = agent_tutor_node(state)

        # The node only sets status and goto — should preserve other fields
        update = result.update
        assert "status" in update
        # These fields are in the state already and should be preserved
        # by the LangGraph runtime because we don't explicitly clear them
        assert len(update) <= 2  # only status and maybe nothing else