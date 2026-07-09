"""Chat node — LangGraph node for streaming chat conversations.

This node handles user chat messages by routing them through the
LangGraph state machine, allowing the InMemorySaver checkpointer to
automatically manage message history.

**How it works**:

    1. Frontend sends a chat message via SSE endpoint
    2. SSE endpoint adds the message to state via ``update_state()``
    3. SSE endpoint invokes the graph → routes to this node
    4. This node reads the pending user message from ``state.messages``
    5. Calls the LLM with streaming
    6. Pushes each token via ``StreamWriter`` (SSE streams them to frontend)
    7. Appends the AI response to ``state.messages``
    8. Checkpointer saves the updated state automatically

**Graph topology**:

    start_router → (normal flow) | (chat_node if new user message)
                → chat_node → (back to previous state)
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import StreamWriter

from code_tutor_agent.config import get_llm
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
    llm = get_llm("agnes-stream", temperature=0.7)

    # Format history for prompt
    history_lines = []
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