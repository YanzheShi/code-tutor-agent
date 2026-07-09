"""Agent dialog node — LangGraph node for multi-turn conversation
to determine the user's problem preferences (topic + difficulty).

This node is the entry point for Agent Tutor Mode sessions.
It manages the initial dialog phase before problem generation.

**Graph topology (agent mode)**:

    START → agent_dialog_node [HERE]
          → planner_node → generator_node → wait_for_submit_node
          → agent_judge_node → agent_tutor_node → (loop back)

**Node behavior**:

    1. First visit (agent_dialog_complete=False):
       - Send initial greeting via build_initial_message()
       - Store in agent_dialog_history + tutor_messages
       - Set status = "dialog"
       - goto="__end__" (graph pauses, waits for SSE chat rounds)

    2. Subsequent visit (after SSE chat endpoint sets agent_dialog_complete=True):
       - Detect completion flag
       - Set status = "awaiting_problem"
       - goto="planner_node" (generation flow starts)

    Between visits, the SSE chat endpoint (/session/{sid}/chat/stream)
    handles the conversation:
      - Saves user messages → calls analyze_user_intent()
      - If not ready → appends AI's next question, updates state
      - If ready → sets agent_dialog_complete=True, topic, difficulty
              → re-invokes graph → agent_dialog_node runs again
              → routes to planner_node
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