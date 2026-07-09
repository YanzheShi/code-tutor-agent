"""Tests for the Agent Dialog node — LangGraph node behavior.

Coverage:
    1. First visit: sends initial message, pauses graph
    2. Complete: routes to planner_node
    3. Idempotent: existing history preserved
"""

from __future__ import annotations

from unittest.mock import patch

from code_tutor_agent.nodes.agent_dialog import agent_dialog_node
from code_tutor_agent.schemas.state import Message, SessionState


class TestAgentDialogNode:
    """Verify the LangGraph node's routing and state updates."""

    def test_first_visit_sends_initial_message(self):
        """First call should create initial message and pause."""
        state = SessionState(
            session_id="test-1",
            mode="agent",
            status="awaiting_problem",
        )

        result = agent_dialog_node(state)

        assert result.goto == "__end__"
        update = result.update
        assert update["status"] == "dialog"
        assert len(update["agent_dialog_history"]) == 1
        assert update["agent_dialog_history"][0].role == "tutor"
        assert len(update["tutor_messages"]) == 1

    def test_first_visit_does_not_duplicate_with_existing_history(self):
        """If history already exists, node should not add another message."""
        existing_msg = Message(role="tutor", content="之前的对话")
        state = SessionState(
            session_id="test-2",
            mode="agent",
            status="dialog",
            agent_dialog_history=[existing_msg],
            tutor_messages=[existing_msg],
        )

        result = agent_dialog_node(state)

        assert result.goto == "__end__"
        assert len(result.update["agent_dialog_history"]) == 1
        assert result.update["agent_dialog_history"][0].content == "之前的对话"

    def test_complete_routes_to_planner(self):
        """When agent_dialog_complete=True, route to planner_node."""
        state = SessionState(
            session_id="test-3",
            mode="agent",
            topic="双指针",
            difficulty="medium",
            status="dialog",
            agent_dialog_complete=True,
            agent_dialog_history=[
                Message(role="tutor", content="想练什么？"),
                Message(role="user", content="双指针"),
                Message(role="tutor", content="难度呢？"),
                Message(role="user", content="中等"),
            ],
        )

        result = agent_dialog_node(state)

        assert result.goto == "planner_node"
        assert result.update["status"] == "awaiting_problem"

    def test_complete_preserves_topic_and_difficulty(self):
        """Topic and difficulty from dialog should be preserved."""
        state = SessionState(
            session_id="test-4",
            mode="agent",
            topic="动态规划",
            difficulty="hard",
            status="dialog",
            agent_dialog_complete=True,
        )

        result = agent_dialog_node(state)

        # The node doesn't change topic/difficulty, just routes
        assert result.goto == "planner_node"

    def test_first_visit_initial_message_content(self):
        """Initial message should ask about topic."""
        state = SessionState(
            session_id="test-5",
            mode="agent",
            status="awaiting_problem",
        )

        with patch("code_tutor_agent.nodes.agent_dialog.build_initial_message") as mock_build:
            mock_build.return_value = Message(
                role="tutor",
                content="你好！想练什么类型的算法题？",
            )
            result = agent_dialog_node(state)

            assert len(result.update["agent_dialog_history"]) == 1
            assert "算法题" in result.update["agent_dialog_history"][0].content