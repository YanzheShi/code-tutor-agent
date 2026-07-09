"""Tests for the Agent Dialog agent — intent analysis and message generation.

Coverage:
    1. build_initial_message() — correct role + content
    2. build_ready_message() — confirmation includes topic/difficulty
    3. analyze_user_intent() — fallback on LLM error
    4. analyze_user_intent() — structured output parsing (mocked LLM)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_tutor_agent.agents.agent_dialog import (
    DialogIntent,
    analyze_user_intent,
    build_initial_message,
    build_ready_message,
)
from code_tutor_agent.schemas.state import Message


class TestBuildInitialMessage:
    """Agent dialog starts with a friendly opening question."""

    def test_returns_tutor_role(self):
        msg = build_initial_message()
        assert msg.role == "tutor"

    def test_contains_topic_question(self):
        msg = build_initial_message()
        # Should ask about what type of problem the user wants
        assert any(kw in msg.content for kw in ["类型", "方向", "什么", "感兴趣"])

    def test_content_is_not_empty(self):
        msg = build_initial_message()
        assert len(msg.content) > 20


class TestBuildReadyMessage:
    """Confirmation message when dialog is complete."""

    def test_includes_topic_and_difficulty(self):
        intent = DialogIntent(topic="双指针", difficulty="medium", is_ready=True)
        msg = build_ready_message("双指针", "medium")
        assert msg.role == "tutor"
        assert "双指针" in msg.content
        assert "中等" in msg.content

    def test_includes_preparation_hint(self):
        intent = DialogIntent(topic="数组", difficulty="easy", is_ready=True)
        msg = build_ready_message("数组", "easy")
        assert "准备" in msg.content or "稍等" in msg.content


class TestAnalyzeUserIntent:
    """LLM-driven intent analysis with fallback behavior."""

    def test_fallback_on_llm_error(self):
        """When LLM fails, should return a safe fallback with is_ready=False."""
        history = [
            Message(role="tutor", content="想练什么类型？"),
            Message(role="user", content="数组"),
        ]

        # Mock get_llm to raise an exception
        with patch("code_tutor_agent.agents.agent_dialog.get_llm") as mock_get_llm:
            mock_get_llm.side_effect = Exception("LLM unavailable")

            result = analyze_user_intent(history)

            assert isinstance(result, DialogIntent)
            assert result.is_ready is False
            assert result.topic == ""
            assert result.difficulty == ""
            assert len(result.next_message) > 0  # fallback message

    def test_passes_history_to_llm(self):
        """Verify the LLM is called with the full conversation transcript."""
        history = [
            Message(role="tutor", content="想练什么类型的题？"),
            Message(role="user", content="想练双指针"),
            Message(role="tutor", content="什么难度？"),
            Message(role="user", content="中等吧"),
        ]

        mock_structured = MagicMock()
        mock_structured.invoke.return_value = DialogIntent(
            topic="双指针",
            difficulty="medium",
            is_ready=True,
            next_message="好的，为你准备一道双指针的中等题！",
        )

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=mock_llm):
            result = analyze_user_intent(history)

            assert result.topic == "双指针"
            assert result.difficulty == "medium"
            assert result.is_ready is True
            assert "好的" in result.next_message

            # Verify LLM was called with structured output
            mock_llm.with_structured_output.assert_called_once()
            # Verify the system prompt + conversation transcript were passed
            call_args = mock_structured.invoke.call_args[0][0]
            messages_text = "".join(str(m) for m in call_args)
            assert "双指针" in messages_text
            assert "中等" in messages_text

    def test_not_ready_when_topic_unclear(self):
        """If topic is not clear, is_ready should be False."""
        history = [
            Message(role="tutor", content="想练什么类型的题？"),
            Message(role="user", content="随便吧"),
        ]

        mock_structured = MagicMock()
        mock_structured.invoke.return_value = DialogIntent(
            topic="",
            difficulty="",
            is_ready=False,
            next_message="那我推荐几个方向：数组、链表、动态规划，你感兴趣哪个？",
        )

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=mock_llm):
            result = analyze_user_intent(history)

            assert result.is_ready is False
            assert result.topic == ""
            assert "推荐" in result.next_message

    def test_empty_history_returns_fallback(self):
        """With empty history (shouldn't happen in practice), LLM still gets called."""
        mock_structured = MagicMock()
        mock_structured.invoke.side_effect = Exception("API error")

        with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=MagicMock()):
            with patch("code_tutor_agent.agents.agent_dialog.get_llm") as mock_get:
                mock_get.side_effect = Exception("API error")
                result = analyze_user_intent([])

                assert result.is_ready is False
                assert len(result.next_message) > 0