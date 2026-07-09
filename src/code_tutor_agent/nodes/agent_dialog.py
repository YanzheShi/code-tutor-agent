"""Agent 对话节点 — 多轮对话 LangGraph 节点，用于确定用户的问题偏好（topic + difficulty）。

该节点是 Agent 导师模式的入口。
它在出题前管理初始对话阶段。

节点流转（详见 docstring 末尾的拓扑图）：
    START → agent_dialog_node → [循环直到对话完成] → planner_node
"""

from __future__ import annotations

import logging

from langgraph.types import Command

from code_tutor_agent.agents.agent_dialog import build_initial_message
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)


def agent_dialog_node(state: SessionState) -> Command:
    """LangGraph node: manage the agent-user dialog before problem generation.

    First call: send greeting, pause graph.
    Second call (after SSE chat completes dialog): route to planner_node.

    Args:
        state: Current session state. Reads ``agent_dialog_complete``,
               ``agent_dialog_history``, ``tutor_messages``.

    Returns:
        Command either pausing the graph (goto=__end__) or
        routing to planner_node.
    """
    logger.info("▶ agent_dialog_node() — complete=%s history=%d",
                 state.agent_dialog_complete, len(state.agent_dialog_history))

    # ── Dialog already completed by SSE chat endpoint → proceed to generate ──
    if state.agent_dialog_complete:
        logger.info(
            "Dialog complete — routing to planner_node (topic=%s, diff=%s)",
            state.topic, state.difficulty,
        )
        return Command(
            update={"status": "awaiting_problem"},
            goto="planner_node",
        )

    # ── First visit: send initial message and pause ──
    if not state.agent_dialog_history:
        msg = build_initial_message()
        state.agent_dialog_history = [msg]
        state.tutor_messages = [msg]
        logger.info("First visit — sent initial message: %s", msg.content[:60])

    return Command(
        update={
            "status": "dialog",
            "agent_dialog_history": state.agent_dialog_history,
            "tutor_messages": state.tutor_messages,
        },
        goto="__end__",
    )