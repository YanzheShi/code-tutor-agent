"""CodeTutor Agent — LangGraph StateGraph 定义与编译。"""
from __future__ import annotations

import logging

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import END, StateGraph

from code_tutor_agent.nodes.generator import generator_node
from code_tutor_agent.nodes.judge import judge_node
from code_tutor_agent.nodes.planner import planner_node
from code_tutor_agent.nodes.tutor import tutor_node
from code_tutor_agent.nodes.critic import critic_node
from code_tutor_agent.profile import update_profile_node
from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node

from code_tutor_agent.nodes.agent_dialog import agent_dialog_node
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.nodes.agent_tutor import agent_tutor_node
from code_tutor_agent.nodes.chat import chat_node

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
    builder.add_node("tutor_node", tutor_node)
    builder.add_node("critic_node", critic_node)
    builder.add_node("update_profile_node", update_profile_node)
    builder.add_node("agent_dialog_node", agent_dialog_node)
    builder.add_node("agent_judge_node", agent_judge_node)
    builder.add_node("agent_tutor_node", agent_tutor_node)
    builder.add_node("chat_node", chat_node)

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
    builder.add_edge("judge_node", "tutor_node")
    builder.add_edge("tutor_node", "update_profile_node")
    builder.add_edge("update_profile_node", "critic_node")
    builder.add_edge("agent_judge_node", "agent_tutor_node")
    # chat_node 回到 END（checkpointer 自动保存状态）
    builder.add_edge("chat_node", END)

    return builder


def compile_graph(
    conn_string: str | None = None,
) -> CompiledStateGraph:
    logger.info("▶ compile_graph() — using InMemorySaver")
    builder = _build_graph()
    checkpointer = InMemorySaver()
    graph = builder.compile(checkpointer=checkpointer)
    logger.info("Graph compiled — InMemorySaver checkpointer")
    return graph