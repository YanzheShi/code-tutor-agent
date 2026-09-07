"""API 路由的共享依赖：get_graph() 单例 + 进度存储器 + 暂停安全状态写入 + 并发护栏。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from code_tutor_agent.config import get_database_url
from code_tutor_agent.graph.graph import compile_graph
from code_tutor_agent.progress import _generation_progress

logger = logging.getLogger(__name__)

# ── Global graph reference (set once at startup) ──
_graph: CompiledStateGraph | None = None

# ── 并发护栏（多用户改造 P3）：LLM 生成 + 判题共用一个信号量，超员排队 ──
# 默认 10，可用 MAX_CONCURRENCY 环境变量调整。排队而非拒绝（429）：
# 10 并发内排队延迟可忽略，且省去前端对 429 的整套重试 UI。
_MAX_CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENCY", "10")))
_concurrency_sem = asyncio.Semaphore(_MAX_CONCURRENCY)


async def run_with_concurrency_limit(fn, *args, **kwargs):
    """在全局并发护栏内执行 fn（通常配合 asyncio.to_thread 使用方）。

    用法：``await run_with_concurrency_limit(asyncio.to_thread, graph.invoke, ...)`` 不行——
    to_thread 需要立即调度；正确用法是把「阻塞调用」包成 lambda 交给本函数在线程池跑：
    ``await run_with_concurrency_limit(_blocking_call, *args)``。
    本函数内部自行用 asyncio.to_thread 执行，调用方不再包 to_thread。
    """
    async with _concurrency_sem:
        return await asyncio.to_thread(fn, *args, **kwargs)


def init_graph() -> CompiledStateGraph:
    """Compile the LangGraph and store the reference. Called once at startup."""
    global _graph
    logger.info("Compiling LangGraph ...")
    conn_string = get_database_url()
    _graph = compile_graph(conn_string=conn_string)
    logger.info("LangGraph ready")
    return _graph


def get_graph() -> CompiledStateGraph:
    """Get the global graph reference. Raises if not initialized."""
    if _graph is None:
        raise RuntimeError("Graph not initialized")
    return _graph


def invoke_graph_tracked(graph: CompiledStateGraph, graph_input, config, entry: str = ""):
    """graph.invoke 的监控包装（docs/monitoring-alerts-design.md §14.3）。

    行为与 graph.invoke 完全一致（异常原样上抛），仅追加埋点：
    graph_ok/graph_fail 计数 + graph_fail streak（连续 ≥2 次 → critical 告警）。
    埋点本身永不抛异常，不影响主流程。
    """
    from code_tutor_agent.monitoring.metrics import record_graph_call

    try:
        result = graph.invoke(graph_input, config)
        record_graph_call(entry, True)
        return result
    except Exception:
        record_graph_call(entry, False)
        raise


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
