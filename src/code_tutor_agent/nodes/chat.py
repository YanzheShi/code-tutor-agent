"""聊天节点 — 流式聊天对话的 LangGraph 节点。

该节点处理用户聊天消息，通过 LangGraph 状态机路由，
让 InMemorySaver checkpointer 自动管理消息历史。

上下文管理（v2）：
    如果 state.context_summary 非空（长对话被压缩后生成的摘要），
    它会被注入到 prompt 的最前面，保证 LLM 不会丢失之前的关键信息。

节点流转：
    start_router → chat_node → END
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import StreamWriter

from code_tutor_agent.config import get_llm
from code_tutor_agent.context_manager import (
    DEFAULT_CONFIG,
    build_transcript_with_budget,
)
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# ── Prompts ──

TUTOR_CHAT_SYSTEM = """你是 AI 编程导师，语气友好、鼓励。
根据对话历史和当前消息给出有帮助的指导。
- 不要直接给出完整代码
- 引导用户自己思考
- 用户可以在这里直接贴代码给你分析，你可以判断代码是否正确
- 回复控制在 200 字以内"""


def chat_node(state: SessionState, writer: StreamWriter) -> dict:
    """Process one user chat message through the graph.

    Reads the latest user message from ``state.messages``, calls the LLM
    with streaming, pushes tokens via ``writer``, and appends the AI
    response to the message list.

    上下文管理：
        如果 session 有 context_summary（来自之前的滑动窗口压缩），
        它会作为对话摘要注入 prompt 开头，保证关键信息不丢失。

    Args:
        state: Current session state. Reads ``messages`` list.
        writer: LangGraph StreamWriter for token-level streaming.

    Returns:
        Partial state update with the updated ``messages`` list.
    """
    logger.info("▶ chat_node() — %d messages in state", len(state.messages))

    # ── Get the latest user message ──
    messages = list(state.messages or [])
    if not messages or not isinstance(messages[-1], (HumanMessage, dict)):
        logger.warning("chat_node: no pending user message")
        return {}

    last = messages[-1]
    user_text = last.content if hasattr(last, "content") else (last.get("content", "") if isinstance(last, dict) else "")

    # ── Build LLM prompt from message history ──
    llm = get_llm(purpose="chat")

    # Format history for prompt, inject context_summary if available
    history_lines = []
    if state.context_summary:
        # 将压缩摘要放在对话历史最前面，保证 LLM 了解之前的上下文
        history_lines.append(f"[对话摘要]\n{state.context_summary}\n")
    for msg in messages[:-1]:  # exclude the latest user message
        role = "用户" if isinstance(msg, HumanMessage) or (isinstance(msg, dict) and msg.get("role") == "user") else "AI导师"
        content = msg.content if hasattr(msg, "content") else msg.get("content", "")
        history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines) if history_lines else "无历史对话"
    prompt = f"## 对话历史\n\n{history_text}\n\n## 用户当前消息\n{user_text}\n\n请回复。"

    # ── Stream LLM response ──
    collected = []
    try:
        for chunk in llm.stream([
            ("system", TUTOR_CHAT_SYSTEM),
            ("human", prompt),
        ]):
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            if token:
                collected.append(token)
                writer({"type": "token", "content": token})
    except Exception as exc:
        logger.warning("chat_node LLM failed: %s", exc)
        fallback = "抱歉，我现在无法回答。请稍后再试。"
        collected.append(fallback)
        writer({"type": "token", "content": fallback})

    # ── Append AI response to messages ──
    response = "".join(collected)
    messages.append(AIMessage(content=response))

    logger.info("chat_node: generated %d chars", len(response))
    return {"messages": messages}