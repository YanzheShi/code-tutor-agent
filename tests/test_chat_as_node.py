"""回归测试：agent-dialog 分支的 graph.update_state 必须带 as_node。

触发场景（2026-07-21）：出题（agent-dialog 模式）调到 chat/stream 时，
graph.update_state(config, {...}) 在图停在 END（无 pending task）时抛
`InvalidUpdateError: Ambiguous update, specify as_node`。
session.py 的 next-problem 用 as_node="agent_dialog_node"，chat 路由的
agent-dialog 写入须保持一致。
"""
import asyncio
import types
from unittest.mock import patch

from fastapi import BackgroundTasks

import code_tutor_agent.api.routers.chat as chat_router
from code_tutor_agent.agents.agent_dialog import DialogIntent
from code_tutor_agent.schemas.state import Message

_READY_MSG = Message(role="tutor", content="generating problem")


class _FakeGraph:
    def __init__(self, state_values: dict):
        self._state = types.SimpleNamespace(values=state_values)
        self.update_calls = []

    def get_state(self, config):
        return self._state

    def update_state(self, config, values, as_node=None):
        self.update_calls.append((values, as_node))
        return None


def _agent_dialog_state(**overrides) -> dict:
    base = {
        "mode": "agent",
        "status": "dialog",
        "agent_dialog_complete": False,
        "topic": "",
        "difficulty": "",
        "agent_dialog_history": [],
        "tutor_messages": [],
        "context_summary": "",
    }
    base.update(overrides)
    return base


def test_chat_agent_dialog_non_ready_update_state_passes_as_node():
    graph = _FakeGraph(_agent_dialog_state())

    non_ready = DialogIntent(is_ready=False, next_message="what topic?")

    with patch.object(chat_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.agents.agent_dialog.analyze_user_intent",
               return_value=non_ready):
        result = asyncio.run(
            chat_router.chat_with_tutor(
                "sess-1", {"message": "practice algo"}, BackgroundTasks()
            )
        )

    assert result["response"] == "what topic?"
    # agent-dialog 分支写了两次 state（首轮落历史 + 末轮回消息），都应带 as_node
    assert len(graph.update_calls) == 2
    for _values, as_node in graph.update_calls:
        assert as_node == "agent_dialog_node"


def test_chat_agent_dialog_ready_update_state_passes_as_node():
    graph = _FakeGraph(_agent_dialog_state())

    ready = DialogIntent(
        is_ready=True, topic="array", difficulty="easy", next_message="generating..."
    )

    with patch.object(chat_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.agents.agent_dialog.analyze_user_intent",
               return_value=ready), \
         patch("code_tutor_agent.agents.agent_dialog.build_ready_message",
               return_value=_READY_MSG), \
         patch("code_tutor_agent.config.get_llm", return_value=None):
        result = asyncio.run(
            chat_router.chat_with_tutor(
                "sess-2", {"message": "array easy"}, BackgroundTasks()
            )
        )

    assert result["response"]
    assert len(graph.update_calls) == 2
    for _values, as_node in graph.update_calls:
        assert as_node == "agent_dialog_node"
