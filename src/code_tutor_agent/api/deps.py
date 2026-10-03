"""API 路由的共享依赖：get_graph() 单例 + 进度存储器 + 暂停安全状态写入 + 并发护栏。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import HTTPException
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


def _queue_limit() -> int:
    """排队上限（F-04 补充，2026-09-08）：排队+执行总数达到
    _MAX_CONCURRENCY + 本值时直接 429「判题繁忙」，防单账号高频提交占满
    全部槽位饿死他人。默认 20，≤0 关闭。惰性读 env 便于测试调整。"""
    raw = os.getenv("MAX_CONCURRENCY_QUEUE", "20").strip()
    try:
        return int(raw)
    except ValueError:
        return 20


# 已进入护栏（排队中 + 执行中）的请求数。仅在事件循环协程内读写
# （检查与自增之间无 await），单线程语义下无竞态。
_active_count = 0


async def run_with_concurrency_limit(fn, *args, **kwargs):
    """在全局并发护栏内执行 fn（通常配合 asyncio.to_thread 使用方）。

    用法：``await run_with_concurrency_limit(asyncio.to_thread, graph.invoke, ...)`` 不行——
    to_thread 需要立即调度；正确用法是把「阻塞调用」包成 lambda 交给本函数在线程池跑：
    ``await run_with_concurrency_limit(_blocking_call, *args)``。
    本函数内部自行用 asyncio.to_thread 执行，调用方不再包 to_thread。

    排队上限（F-04 补充）：护栏内总数（排队+执行中）达 _MAX_CONCURRENCY +
    MAX_CONCURRENCY_QUEUE 时抛 429，超出部分不排队直接拒绝。
    """
    global _active_count
    _qlimit = _queue_limit()
    if _qlimit > 0 and _active_count >= _MAX_CONCURRENCY + _qlimit:
        raise HTTPException(429, "当前判题繁忙，请稍后再试")
    _active_count += 1
    try:
        async with _concurrency_sem:
            return await asyncio.to_thread(fn, *args, **kwargs)
    finally:
        _active_count -= 1


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


# ── update_state 的 as_node 选择（2026-10-01 事故修复）──
# 两条硬约束（缺一不可）：
#   1) **永远不要省略 as_node**。`graph.update_state(config, values)` 会让 langgraph
#      自己推断写入者；而推断在「versions_seen 内层全空」时会退化到
#      ``as_node = self.input_channels``，本图即 ``"__start__"``。``__start__`` 的
#      writer 恰好就是 ``start_router`` 那条条件边 —— 于是「写状态」顺带把 router
#      真的执行了一遍，把 ``branch:to:agent_dialog_node`` 写进 checkpoint，
#      ``next`` 被钉成 ``('agent_dialog_node',)``：/run 400「会话未在等待提交」，
#      且 run.py / session.py 的卡死兜底判据 ``not state.next`` 不再成立 →
#      **整道题永久判不了**，此后每条对话消息都会再钉一次。
#   2) 出题镜像注入会让真实会话正好落进「versions_seen 全空」这个危险状态
#      （见 api/services/generation.py::mirror_state），所以这条路径必须显式给 as_node。
AS_NODE_AWAITING_SUBMIT = "agent_tutor_node"
AS_NODE_NEUTRAL = "generator_node"


def write_as_node(has_problem: bool) -> str:
    """按会话是否已有题目，挑一个安全的 as_node 供 update_state 使用。

    为什么是这两个节点：
      - 有题 → ``agent_tutor_node``：它的**静态边**指向 ``wait_for_submit_node``，
        写完后 langgraph 推出的 ``next`` 正好是 ``('wait_for_submit_node',)`` ——
        会话就此真正处于「等待提交」，/run 与 /submit 直接走正常链路，
        不必依赖 API 层的卡死兜底（兜底要求 next 为空，一旦 next 变成别的节点就失效）。
      - 无题（对话 / 出题中）→ ``generator_node``：它只有 ``Command(goto)`` 出边，
        langgraph 推不出 next → ``next`` 保持 ``()``，不会给一个还没有题的会话
        装上假的「等待提交」态。
    """
    return AS_NODE_AWAITING_SUBMIT if has_problem else AS_NODE_NEUTRAL


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
    graph 已停在 END）回退到 update_state。

    2026-10-01 补充（做题界面「运行」永久 400 事故）：非暂停态**不能**省略
    as_node——出题镜像注入后的会话 versions_seen 全空，省略会让 langgraph 把
    as_node 推断成 ``__start__`` 并真的执行 start_router，把 next 钉死成
    ``('agent_dialog_node',)``（详见上方 AS_NODE_* 注释）。故本函数一律显式传 as_node。
    """
    try:
        state = graph.get_state(config)
        next_nodes = state.next or ()
        has_problem = bool(state.values.get("problem"))
    except Exception:
        next_nodes = ()
        has_problem = False

    if "wait_for_submit_node" in next_nodes:
        graph.invoke(
            Command(update=values, goto="wait_for_submit_node"),
            config,
        )
    else:
        graph.update_state(
            config, values, as_node=as_node or write_as_node(has_problem)
        )
