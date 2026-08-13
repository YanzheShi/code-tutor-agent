"""CodeTutor Agent — LangGraph StateGraph 定义与编译。"""
from __future__ import annotations

import logging
import os
import sqlite3

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import StateGraph, END
from langgraph.store.memory import InMemoryStore

from code_tutor_agent.nodes.generator import generator_node
from code_tutor_agent.nodes.planner import planner_node
from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node
from code_tutor_agent.nodes.critic import critic_node
from code_tutor_agent.profile import update_profile_node
from code_tutor_agent.nodes.agent_dialog import agent_dialog_node
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.nodes.agent_tutor import agent_tutor_node

from code_tutor_agent.schemas.state import SessionState

load_dotenv()
logger = logging.getLogger(__name__)


def agent_judge_router(state: SessionState) -> str:
    """路由 agent 判题节点的出口（agent_judge_node 已不再 return Command(goto)）。

    规则：
        status == "error"                                → END（容错，不继续链路）
        last_verdict != "AC"（WA/RE/TLE/CE）            → agent_tutor_node（给重试指导）
        AC 且（运行 is_run 或 sample scope）            → wait_for_submit_node（不写画像、不 done）
        AC 且 真实提交（full + 非运行）                 → update_profile_node（写 v2 画像 → critic）
    """
    if state.status == "error":
        return END
    verdict = state.last_verdict
    is_run = state.submissions[-1].is_run if state.submissions else False
    if verdict != "AC":
        return "agent_tutor_node"
    if is_run or state.judge_scope == "sample":
        # 运行（sample+AC）不写画像、不 done，保持循环
        return "wait_for_submit_node"
    return "update_profile_node"


def _build_graph() -> StateGraph:
    builder = StateGraph(SessionState)

    # ── Register all nodes ──
    builder.add_node("planner_node", planner_node)
    builder.add_node("generator_node", generator_node)
    builder.add_node("wait_for_submit_node", wait_for_submit_node)
    builder.add_node("critic_node", critic_node)
    builder.add_node("update_profile_node", update_profile_node)

    # agent 模式节点（normal 模式的 judge/tutor/constitutional_guard 已删除）
    builder.add_node("agent_dialog_node", agent_dialog_node)
    builder.add_node("agent_judge_node", agent_judge_node)
    builder.add_node("agent_tutor_node", agent_tutor_node)

    # ── Start router: mode 已在 API 层强制为 agent，保留防御性分支 ──
    def start_router(state: SessionState) -> str:
        if state.mode == "agent":
            logger.info("start_router → agent mode, goto=agent_dialog_node")
            return "agent_dialog_node"
        logger.info("start_router → non-agent fallback, goto=planner_node")
        return "planner_node"

    def wait_for_submit_router(state: SessionState) -> str:
        # 换题路径：wait 收到 abandon resume 载荷后置 pending_abandon=True，
        # 直接进 critic_node 走 flush+planner+generator 换题（不产生提交、不判题）。
        if state.pending_abandon:
            logger.info("wait_for_submit_router → pending_abandon, goto=critic_node")
            return "critic_node"
        # normal 的 judge_node 已删除，agent 模式统一走 agent_judge_node
        return "agent_judge_node"

    # ── Edges ──
    # 连线原则（langgraph 1.2.7 语义）：返回 Command(goto) 的节点一律不加静态出边，
    # 否则静态边与 Command 同时生效导致双节点执行（2026-08-04 历史坑）。因此：
    #   - __start__ / wait_for_submit_node / agent_judge_node 用条件边（节点返回纯 dict）
    #   - update_profile_node / agent_tutor_node 是确定性单出口，改静态边（节点返回纯 dict）
    #   - planner_node / generator_node / agent_dialog_node / critic_node 保留 Command(goto)
    #     （它们含分支/暂停/错误路径，改静态边会与错误/分支 Command 冲突）。
    builder.add_conditional_edges("__start__", start_router)
    builder.add_conditional_edges("wait_for_submit_node", wait_for_submit_router)
    builder.add_conditional_edges("agent_judge_node", agent_judge_router)

    # 确定性线性链 → 静态边（节点不再 return Command(goto)）
    builder.add_edge("update_profile_node", "critic_node")
    builder.add_edge("agent_tutor_node", "wait_for_submit_node")

    return builder


def compile_graph(
    conn_string: str | None = None,
) -> CompiledStateGraph:
    builder = _build_graph()

    if conn_string:
        logger.info(f"compile_graph() — using SqliteSaver ({conn_string})")
        os.makedirs(os.path.dirname(conn_string) or ".", exist_ok=True)
        conn = sqlite3.connect(conn_string, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        checkpointer = SqliteSaver(conn)
    else:
        logger.warning("compile_graph() — no conn_string, falling back to InMemorySaver")
        checkpointer = InMemorySaver()

    store = InMemoryStore()
    graph = builder.compile(checkpointer=checkpointer, store=store)
    logger.info(f"Graph compiled — checkpointer={type(checkpointer).__name__}")
    return graph
