"""Tests for the Agent Dialog agent — intent analysis and message generation.

Coverage:
    1. build_initial_message() — correct role + content
    2. build_ready_message() — confirmation includes topic/difficulty
    3. analyze_user_intent() — fallback on LLM error
    4. analyze_user_intent() — structured output parsing (mocked LLM)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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

    @pytest.mark.asyncio
    async def test_fallback_on_llm_error(self):
        """When LLM fails, should return a safe fallback with is_ready=False."""
        history = [
            Message(role="tutor", content="想练什么类型？"),
            Message(role="user", content="我想练点题"),  # 无明确知识点 → 兜底 topic 为空
        ]

        # Mock get_llm to raise an exception
        with patch("code_tutor_agent.agents.agent_dialog.get_llm") as mock_get_llm:
            mock_get_llm.side_effect = Exception("LLM unavailable")

            result = await analyze_user_intent(history)

            assert isinstance(result, DialogIntent)
            assert result.is_ready is False
            assert result.topic == ""
            assert result.difficulty == ""
            assert len(result.next_message) > 0  # fallback message

    @pytest.mark.asyncio
    async def test_passes_history_to_llm(self):
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
        # 工具循环：bind_tools 返回自身；invoke 返回无 tool_calls → 直接进结构化输出
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(tool_calls=None)
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=mock_llm):
            result = await analyze_user_intent(history)

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

    @pytest.mark.asyncio
    async def test_not_ready_when_topic_unclear(self):
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
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(tool_calls=None)
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=mock_llm):
            result = await analyze_user_intent(history)

            assert result.is_ready is False
            assert result.topic == ""
            assert "推荐" in result.next_message

    @pytest.mark.asyncio
    async def test_empty_history_returns_fallback(self):
        """With empty history, get_llm fails → fallback is returned safely."""
        with patch("code_tutor_agent.agents.agent_dialog.get_llm") as mock_get:
            mock_get.side_effect = Exception("API error")
            result = await analyze_user_intent([])

            assert result.is_ready is False
            assert len(result.next_message) > 0

    @pytest.mark.asyncio
    async def test_leetcode_url_triggers_parse_and_forces_ready(self):
        """贴 LeetCode 链接时：必须调用 parse_leetcode，且解析成功后强制 is_ready=True。

        即使结构化 LLM 把 is_ready 判为 False（如反问"先听思路还是直接做"），
        也不应停在对话态 —— 解析成功的 LeetCode 题目即视为就绪，应直接触发出题。
        这是修复"给了链接却不出题"回归的关键断言。
        """
        history = [
            Message(role="tutor", content="想练什么类型的题？"),
            Message(
                role="user",
                content="这个问题 https://leetcode.cn/problems/palindrome-number/description/",
            ),
        ]

        lc_payload = json.dumps(
            {
                "title": "Palindrome Number",
                "difficulty": "easy",
                "description": "判断一个整数是否为回文数",
                "examples": [],
                "starter_code": "class Solution:",
            },
            ensure_ascii=False,
        )

        # 工具循环：第一次 invoke 返回带 parse_leetcode 的 tool_call
        tool_call_msg = MagicMock()
        tool_call_msg.tool_calls = [
            {
                "name": "parse_leetcode",
                "args": {
                    "url": "https://leetcode.cn/problems/palindrome-number/description/"
                },
                "id": "call_1",
            }
        ]

        # 结构化输出：故意返回 is_ready=False，模拟 LLM 没把链接视为就绪
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = DialogIntent(
            topic="",
            difficulty="",
            is_ready=False,
            next_message="你想先听思路还是直接做？",
        )

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.return_value = tool_call_msg
        mock_llm.with_structured_output.return_value = mock_structured

        with patch(
            "code_tutor_agent.agents.agent_dialog.get_llm", return_value=mock_llm
        ), patch(
            "code_tutor_agent.agents.agent_dialog.parse_leetcode",
            new=AsyncMock(return_value=lc_payload),
        ):
            result = await analyze_user_intent(history)

        # 核心断言：解析成功 → 强制就绪，路由层才会转入出题
        assert result.source == "leetcode"
        assert result.is_ready is True, (
            "LeetCode 解析成功必须强制 is_ready=True，"
            "否则路由层停在对话态、不出题"
        )
        assert result.leetcode_payload == lc_payload
        assert "Palindrome Number" in result.leetcode_payload