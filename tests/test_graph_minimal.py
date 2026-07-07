"""D1 integration test — LangGraph graph flow.

Tests the minimum viable graph lifecycle:
  1. Graph compiles with checkpointer
  2. First invoke → runs planner → generator → pauses at wait_for_submit_node
  3. Resume with user code → judge → tutor → pauses again or END

This test requires a working LLM endpoint (set in .env).
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from code_tutor_agent.graph.graph import compile_graph
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def graph() -> CompiledStateGraph:
    """Compile the graph once per test module."""
    return compile_graph()


# ──────────────────────────────────────────────
#  Smoke tests (no LLM needed)
# ──────────────────────────────────────────────


class TestGraphTopology:
    """Verify the graph structure is correct."""

    def test_compiles(self, graph: CompiledStateGraph):
        assert graph is not None
        assert "planner_node" in graph.nodes
        assert "generator_node" in graph.nodes
        assert "wait_for_submit_node" in graph.nodes
        assert "judge_node" in graph.nodes
        assert "tutor_node" in graph.nodes

    def test_checkpointer_is_sqlite(self, graph: CompiledStateGraph):
        assert isinstance(graph.checkpointer, SqliteSaver)


# ──────────────────────────────────────────────
#  Full flow test (requires LLM credentials)
# ──────────────────────────────────────────────

LLM_SKIP_REASON = (
    "Skipping LLM-dependent test.  "
    "Set AGNES_MODEL / AGNES_BASE_URL / AGNES_API_KEY in .env to run."
)


@pytest.mark.skipif(
    not os.getenv("AGNES_API_KEY"),
    reason=LLM_SKIP_REASON,
)
class TestGraphFullFlow:
    """Test the full graph lifecycle with a real LLM call."""

    def test_full_session_flow(self, graph: CompiledStateGraph):
        """Complete flow: create session → submit wrong code → get hint.

        This tests the full START → planner → generator → wait_for_submit
        → judge → tutor → wait_for_submit cycle.
        """
        session_id = f"test-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": session_id}}
        initial = SessionState(session_id=session_id)

        # ── Step 1: Create session, run to first interrupt ──
        result = graph.invoke(initial.model_dump(), config)

        state = graph.get_state(config)
        values = state.values

        assert state.next is not None, "Expected graph to be paused"
        assert values["session_id"] == session_id
        assert values["problem"] is not None, "Expected a problem to be generated"
        assert values["problem"].title, "Problem should have a title"
        assert values["problem"].description, "Problem should have a description"
        assert values["status"] == "awaiting_submit"

        logger.info("Step 1 ✓ — problem=%s", values["problem"].title)

        # ── Step 2: Submit deliberately wrong code ──
        bad_code = ("class Solution:\n"
                    "    def solve(self, nums, target):\n"
                    "        return []\n")

        result = graph.invoke(
            Command(resume={"code": bad_code, "language": "python"}),
            config,
        )

        state = graph.get_state(config)
        values = state.values

        # Should have a verdict
        assert values["last_verdict"] is not None
        assert values["last_verdict"] in ("WA", "AC", "RE", "TLE")

        # Should have tutor messages
        assert len(values.get("tutor_messages", [])) > 0

        # Should be paused again (status=awaiting_submit) or done
        if values["last_verdict"] == "AC":
            assert values["status"] == "done"
        else:
            assert values["hint_level"] >= 1
            assert values["status"] == "awaiting_submit"

        logger.info(
            "Step 2 ✓ — verdict=%s hint_level=%d",
            values["last_verdict"], values["hint_level"],
        )

        # ── Step 3: If paused, submit again (or verify done) ──
        if values["status"] == "done":
            return

        # Submit the same bad code again (should get hint level 2)
        result = graph.invoke(
            Command(resume={"code": bad_code, "language": "python"}),
            config,
        )

        state = graph.get_state(config)
        values = state.values

        # Tutor should have escalated
        assert values["hint_level"] >= 2
        assert len(values.get("tutor_messages", [])) >= 2

        logger.info(
            "Step 3 ✓ — hint_level=%d messages=%d",
            values["hint_level"], len(values["tutor_messages"]),
        )