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

    def planner_router(state: SessionState) -> str:
        if state.problem:
            return "wait_for_submit_node"
        return "generator_node"

    def wait_for_submit_router(state: SessionState) -> str:
        if state.mode == "agent":
            return "agent_judge_node"
        return "judge_node"

    # ── Edges ──
    builder.add_conditional_edges("__start__", start_router)
    builder.add_conditional_edges("planner_node", planner_router)
    builder.add_edge("generator_node", "wait_for_submit_node")
    builder.add_conditional_edges("wait_for_submit_node", wait_for_submit_router)

    # Judge → tutor_router (WA 路径经过 L0-L4 决策)
    builder.add_edge("judge_node", "tutor_router_node")

    # tutor_router → tutor_node (CONTINUE/ESCALATE) 或 wait_for_submit (RESOLVED)
    # 由 tutor_router_node 的 Command(goto=...) 动态路由

    # tutor_node (WA 路径) → constitutional_guard_node → wait_for_submit_node（重新暂停等提交）
    # tutor_node (AC 路径) → update_profile_node（由 tutor_node 的 Command 动态路由）
    # 注意：constitutional_guard_node 通过 Command(goto="wait_for_submit_node") 动态路由，
    # 不需要静态边（否则会与静态边冲突，导致两个节点都执行）。
    builder.add_edge("update_profile_node", "critic_node")
    # critic_node 动态路由：AC/ABANDON → planner, WA → wait_for_submit

    builder.add_edge("agent_judge_node", "agent_tutor_node")
    # agent_tutor_node 动态路由：AC → planner, WA → wait_for_submit
    # chat_node 回到 END（checkpointer 自动保存状态）
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