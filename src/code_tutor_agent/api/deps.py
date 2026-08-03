"""API 路由的共享依赖：get_graph() 单例 + 进度存储器 + 暂停安全状态写入。"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

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


def pause_safe_update(
    graph: CompiledStateGraph,
    config: dict,
    values: dict[str, Any],
    as_node: str | None = None,
) -> None:
    """在可能处于 interrupt 暂停态的会话上安全地写状态。

    背景（2026-08-04 run/submit 交互 bug 修复）：
    graph 暂停在 ``wait_for_submit_node`` 的 ``interrupt()`` 期间，直接调
    ``graph.update_state(...)`` 会触发 langgraph 的 as_node 推断副作用，把
    挂起的中断任务从新 checkpoint 上丢掉；此后 ``/submit`` 的
    ``Command(resume=...)`` 找不到可恢复的中断，直接空转返回旧状态，判题不执行。

    修复：暂停在 wait_for_submit_node 时，改用
    ``invoke(Command(update=values, goto="wait_for_submit_node"))`` ——
    写入状态的同时重新触发 wait 节点、重装 interrupt。非暂停态（如对话阶段
    graph 已停在 END）回退到普通 update_state。
    """
    try:
        next_nodes = graph.get_state(config).next or ()
    except Exception:
        next_nodes = ()

    if "wait_for_submit_node" in next_nodes:
        graph.invoke(
            Command(update=values, goto="wait_for_submit_node"),
            config,
        )
    elif as_node is not None:
        graph.update_state(config, values, as_node=as_node)
    else:
        graph.update_state(config, values)
