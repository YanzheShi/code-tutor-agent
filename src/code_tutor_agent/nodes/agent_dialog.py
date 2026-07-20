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

    # ── Dialog not yet complete: pause, preserving any existing history ──
    if not state.agent_dialog_history:
        msg = build_initial_message()
        logger.info("First visit — sent initial message: %s", msg.content[:60])
        hist = [msg]
        tut = [msg]
    else:
        # 重入对话（如 agent 模式「下一题 / 放弃」）：保留已有历史，
        # 追加引导语提示用户选择下一题方向，避免对话静默无引导
        from code_tutor_agent.schemas.state import Message as StateMessage
        prompt_msg = StateMessage(
            role="tutor",
            content="上一题已完成！接下来想练习什么类型的算法题？比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？\n\n也可以直接把一道 LeetCode 题目链接发给我，我们一起练 👇",
        )
        hist = list(state.agent_dialog_history) + [prompt_msg]
        tut = list(state.tutor_messages) + [prompt_msg]
        logger.info("Re-enter dialog — appended next-problem prompt")

    return Command(
        update={
            "status": "dialog",
            "agent_dialog_history": hist,
            "tutor_messages": tut,
        },
        goto="__end__",
    )