"""API 路由的共享依赖：get_graph() 单例 + 进度存储器。"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from code_tutor_agent.config import get_checkpoint_db_path
from code_tutor_agent.graph.graph import compile_graph
from code_tutor_agent.progress import _generation_progress

logger = logging.getLogger(__name__)

# ── Global graph reference (set once at startup) ──
_graph: CompiledStateGraph | None = None


def init_graph() -> CompiledStateGraph:
    """Compile the LangGraph and store the reference. Called once at startup."""
    global _graph
    logger.info("Compiling LangGraph ...")
    conn_string = get_checkpoint_db_path()
    _graph = compile_graph(conn_string=conn_string)
    logger.info("LangGraph ready")
    return _graph


def get_graph() -> CompiledStateGraph:
    """Get the global graph reference. Raises if not initialized."""
    if _graph is None:
        raise RuntimeError("Graph not initialized")
    return _graph
