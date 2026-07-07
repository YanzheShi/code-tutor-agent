"""CodeTutor Agent — LangGraph StateGraph definition.

D1 minimal topology (one session round):

.. code-block::

    START ──→ planner_node ──→ generator_node ──→ wait_for_submit_node
                                                      │
                                                 [interrupt — user writes code]
                                                      │
                                                      ▼
                                                judge_node
                                                      │
                                                      ▼
                                                tutor_node
                                                      │
                                          ┌───────────┴───────────┐
                                          │                       │
                                     (AC) │                   (WAIT)
                                          ▼                       ▼
                                     planner_node     wait_for_submit_node
                                                          (next round)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import END, StateGraph

from code_tutor_agent.nodes.generator import generator_node as _raw_generator
from code_tutor_agent.nodes.judge import judge_node
from code_tutor_agent.nodes.planner import planner_node
from code_tutor_agent.nodes.tutor import tutor_node
from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node
from code_tutor_agent.schemas.state import SessionState

load_dotenv()
logger = logging.getLogger(__name__)

# ── Checkpointer path (relative to project root) ──
_CHECKPOINT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "data", "checkpoints", "checkpoints.sqlite",
)


def _build_graph(progress_cb: Callable[[str], None] | None = None) -> StateGraph:
    """Construct the ``StateGraph`` with all nodes and edges.

    Args:
        progress_cb: Optional callback fired by generator_node to report
                     progress messages (shown in the frontend during generation).
    """
    if progress_cb is None:
        progress_cb = lambda msg: None  # no-op by default

    # Bind progress callback into generator
    def generator_wrapper(state: SessionState) -> dict[str, Any]:
        logger.info("▶ generator_wrapper()")
        return _raw_generator(state, progress_cb)

    builder = StateGraph(SessionState)

    # ── Register nodes ──
    builder.add_node("planner_node", planner_node)
    builder.add_node("generator_node", generator_wrapper)
    builder.add_node("wait_for_submit_node", wait_for_submit_node)
    builder.add_node("judge_node", judge_node)
    builder.add_node("tutor_node", tutor_node)

    # ── Conditional router: planner can skip generator if problem already loaded ──
    def planner_router(state: SessionState) -> str:
        """Route to generator_node unless a problem is already loaded."""
        if state.problem:
            logger.info("planner_router → problem loaded, goto=wait_for_submit_node")
            return "wait_for_submit_node"
        logger.info("planner_router → goto=generator_node")
        return "generator_node"

    # ── Edges ──
    builder.add_edge("__start__", "planner_node")
    builder.add_conditional_edges("planner_node", planner_router)
    builder.add_edge("generator_node", "wait_for_submit_node")
    builder.add_edge("wait_for_submit_node", "judge_node")
    builder.add_edge("judge_node", "tutor_node")
    # tutor_node uses Command(goto=...) to route back to planner or wait

    return builder


def compile_graph(
    conn_string: str | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> CompiledStateGraph:
    """Build and compile the graph with a SqliteSaver checkpointer.

    Opens a persistent SQLite connection that stays alive for the
    lifetime of the returned graph object.

    Args:
        conn_string: SQLite connection string.  Defaults to
                     ``checkpoints.sqlite`` in the project root.
        progress_cb: Optional callback for generation progress messages.

    Returns:
        A compiled ``StateGraph`` ready for ``invoke()``.
    """
    logger.info("▶ compile_graph() — conn=%s", conn_string or _CHECKPOINT_PATH)
    builder = _build_graph(progress_cb)
    conn_string = conn_string or _CHECKPOINT_PATH
    conn = sqlite3.connect(conn_string, check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    graph = builder.compile(checkpointer=checkpointer)

    logger.info("Graph compiled — checkpointer=%s (conn open=%s)", conn_string, True)
    return graph