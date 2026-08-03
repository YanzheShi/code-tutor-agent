"""CodeTutor Agent — LangGraph StateGraph 定义与编译。"""
from __future__ import annotations

import logging
import os
import sqlite3

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import END, StateGraph
from langgraph.store.memory import InMemoryStore

from code_tutor_agent.nodes.generator import generator_node
from code_tutor_agent.nodes.judge import judge_node
from code_tutor_agent.nodes.planner import planner_node
from code_tutor_agent.nodes.tutor import tutor_node
from code_tutor_agent.nodes.tutor_router import tutor_router_node
from code_tutor_agent.nodes.critic import critic_node
from code_tutor_agent.profile import update_profile_node
from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node

from code_tutor_agent.nodes.agent_dialog import agent_dialog_node
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.nodes.agent_tutor import agent_tutor_node
from code_tutor_agent.nodes.chat import chat_node
from code_tutor_agent.nodes.constitutional_guard import constitutional_guard_node

from code_tutor_agent.schemas.state import SessionState

load_dotenv()
logger = logging.getLogger(__name__)


def _build_graph() -> StateGraph:
    builder = StateGraph(SessionState)

    # ── Register all nodes ──
    builder.add_node("planner_node", planner_node)
    builder.add_node("generator_node", generator_node)
    builder.add_node("wait_for_submit_node", wait_for_submit_node)
    builder.add_node("judge_node", judge_node)
    builder.add_node("tutor_router_node", tutor_router_node)
    builder.add_node("tutor_node", tutor_node)
    builder.add_node("critic_node", critic_node)
    builder.add_node("update_profile_node", update_profile_node)
    builder.add_node("agent_dialog_node", agent_dialog_node)
    builder.add_node("agent_judge_node", agent_judge_node)
    builder.add_node("agent_tutor_node", agent_tutor_node)
    builder.add_node("chat_node", chat_node)
    builder.add_node("constitutional_guard_node", constitutional_guard_node)

    # ── Start router: chat, agent mode, or normal ──
    def start_router(state: SessionState) -> str:
        # 如果有未回复的用户消息 → 路由到 chat_node
        msgs = state.messages or []
        if msgs:
            last = msgs[-1]
            if isinstance(last, (HumanMessage, dict)):
                role = last.get("role", "") if isinstance(last, dict) else "human"
                if isinstance(last, HumanMessage) or role == "user":
                    logger.info("start_router → chat message pending, goto=chat_node")
                    return "chat_node"
        # Agent 模式
        if state.mode == "agent":
            logger.info("start_router → agent mode, goto=agent_dialog_node")
            return "agent_dialog_node"
        logger.info("start_router → normal mode, goto=planner_node")
        return "planner_node"

    def wait_for_submit_router(state: SessionState) -> str:
        # 换题路径：wait 收到 abandon resume 载荷后置 pending_abandon=True，
        # 直接进 critic_node 走 flush+planner+generator 换题（不产生提交、不判题）。
        if state.pending_abandon:
            logger.info("wait_for_submit_router → pending_abandon, goto=critic_node")
            return "critic_node"
        if state.mode == "agent":
            return "agent_judge_node"
        return "judge_node"

    # ── Edges ──
    # 连线原则（langgraph 1.2.7 语义，scripts/verify_command_edge_conflict.py 实测）：
    # 节点返回 Command(goto=...) 时，静态边**不会**被覆盖——两者同时生效，
    # 目标不同时两个目标节点都会执行。因此返回 Command 的节点一律不加静态出边。
    # 历史上 judge→tutor_router / agent_judge→agent_tutor / update_profile→critic
    # 三处「静态边 + Command」曾导致双节点执行（2026-08-04 移除）。
    builder.add_conditional_edges("__start__", start_router)
    builder.add_conditional_edges("wait_for_submit_node", wait_for_submit_router)

    # 以下节点全部经 Command(goto=...) 动态路由，无静态边：
    #   planner_node            → generator_node | wait_for_submit_node
    #   generator_node          → wait_for_submit_node
    #   judge_node              → tutor_node
    #   tutor_node              → constitutional_guard_node | update_profile_node
    #   constitutional_guard_node → wait_for_submit_node
    #   update_profile_node     → critic_node
    #   critic_node             → wait_for_submit_node | planner_node
    #   agent_judge_node        → update_profile_node(AC) | agent_tutor_node(WA)
    #   agent_tutor_node        → wait_for_submit_node(WA) | __end__(AC 兜底，正常不可达)
    # wait_for_submit_node 的条件路由（wait_for_submit_router）：
    #   pending_abandon → critic_node（换题）| agent → agent_judge_node | judge_node
    # 注：tutor_router_node 已注册但当前无入边（原 judge→tutor_router 静态边与
    # judge 的 Command(goto=tutor_node) 冲突导致 tutor 双跑，已移除），保留备用。

    # chat_node 返回普通 dict，走静态边回 END（checkpointer 自动保存状态）
    builder.add_edge("chat_node", END)

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