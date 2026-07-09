"""Chat router — streaming + non-streaming chat endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from starlette.responses import StreamingResponse

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{sid}/chat/stream")
async def chat_with_tutor_stream(sid: str, body: dict):
    """Streaming chat with the AI tutor via SSE."""
    from code_tutor_agent.config import get_llm
    from code_tutor_agent.agents.agent_dialog import (
        analyze_user_intent,
        stream_dialog_response,
        build_ready_message,
    )

    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}
    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    message = (body or {}).get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message is empty")

    values = state.values
    status = values.get("status", "")
    mode = values.get("mode", "")
    agent_done = values.get("agent_dialog_complete", False)

    # Agent dialog mode: only if not already complete
    if status == "dialog" and mode == "agent" and not agent_done:
        history = list(values.get("agent_dialog_history", []))
        history.append(Message(role="user", content=message))
        graph.update_state(config, {"agent_dialog_history": history, "tutor_messages": history})

        intent = analyze_user_intent(history)

        async def dialog_event_stream():
            nonlocal history

            if intent.is_ready:
                topic = intent.topic or values.get("topic", "数组")
                difficulty = intent.difficulty or values.get("difficulty", "easy")
                ready_msg = build_ready_message(topic, difficulty)
                history.append(ready_msg)

                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "agent_dialog_complete": True,
                    "topic": topic,
                    "difficulty": difficulty,
                    "tutor_messages": history,
                })

                for char in intent.next_message or ready_msg.content:
                    yield f"data: {char}\n\n"

                import asyncio as _asyncio
                async def _safe_invoke():
                    try:
                        # invoke(None, config) doesn't pick up update_state changes
                        # Pass current checkpoint state explicitly
                        cur = graph.get_state(config)
                        await _asyncio.to_thread(graph.invoke, dict(cur.values), config)
                    except Exception as e:
                        logger.error("Background graph.invoke failed: %s", e, exc_info=True)
                _asyncio.create_task(_safe_invoke())
            else:
                collected = []
                async for token in stream_dialog_response(history):
                    collected.append(token)
                    yield f"data: {token}\n\n"

                streamed_text = "".join(collected)
                history.append(Message(role="tutor", content=streamed_text))
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "tutor_messages": history,
                })

            yield "data: __DONE__\n\n"

        return StreamingResponse(
            dialog_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Normal tutoring chat ──
    # Check if graph is paused at an interrupt (wait_for_submit_node).
    # If so, astream won't work — fall back to direct LLM streaming.
    state_snap = graph.get_state(config)
    has_interrupt = bool(state_snap.next)

    if has_interrupt:
        # Direct LLM streaming (graph is at interrupt, can't astream chat)
        from code_tutor_agent.config import get_llm
        llm = get_llm("agnes-stream", temperature=0.7)

        # Reflect recent messages in prompt
        problem = values.get("problem")
        title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

        async def direct_stream():
            prompt = (
                f"你是一个编程导师。用户正在做一道算法题「{title}」。\n"
                f"用户当前的消息：{message}\n\n"
                f"请给出有帮助的指导和建议，不要直接给出完整代码。回复控制在 200 字以内。"
            )
            full = []
            try:
                async for chunk in llm.astream(prompt):
                    token = chunk.content if hasattr(chunk, "content") else str(chunk)
                    if token:
                        full.append(token)
                        yield f"data: {token}\n\n"
            except Exception as exc:
                logger.warning("Direct chat LLM failed: %s", exc)
                yield "data: 【抱歉，我现在无法回答。请稍后再试。】\n\n"

            # Save to state manually
            reply = "".join(full)
            tutor_msgs = list(values.get("tutor_messages", []))
            tutor_msgs.append({"role": "user", "content": message})
            tutor_msgs.append({"role": "tutor", "content": reply})
            try:
                graph.update_state(config, {"tutor_messages": tutor_msgs})
            except Exception as exc:
                logger.warning("Failed to save chat: %s", exc)
            yield "data: __DONE__\n\n"

        return StreamingResponse(direct_stream(), media_type="text/event-stream")

    # Graph is not at interrupt — use chat_node via astream
    current_msgs = list(values.get("messages", []))
    current_msgs.append(HumanMessage(content=message))
    graph.update_state(config, {
        "messages": current_msgs,
        "tutor_messages": values.get("tutor_messages", []) + [{"role": "user", "content": message}],
    })

    async def event_stream():
        collected = []
        try:
            async for event in graph.astream(None, config, stream_mode="custom"):
                if isinstance(event, dict) and event.get("type") == "token":
                    token = event["content"]
                    if token:
                        collected.append(token)
                        yield f"data: {token}\n\n"
        except Exception as exc:
            logger.warning("Graph chat streaming failed: %s", exc)
            yield "data: 【抱歉，我现在无法回答。请稍后再试。】\n\n"

        try:
            final_state = graph.get_state(config)
            final_msgs = final_state.values.get("messages", [])
            if final_msgs:
                last = final_msgs[-1]
                ai_content = last.content if hasattr(last, "content") else (last.get("content", "") if isinstance(last, dict) else "")
                tutor_msgs = list(values.get("tutor_messages", []))
                tutor_msgs.append({"role": "tutor", "content": ai_content})
                graph.update_state(config, {"tutor_messages": tutor_msgs})
        except Exception as exc:
            logger.warning("Failed to sync chat state: %s", exc)

        yield "data: __DONE__\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{sid}/chat")
async def chat_with_tutor(sid: str, body: dict):
    """Non-streaming chat with the AI tutor."""
    from code_tutor_agent.config import get_llm

    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}
    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    message = (body or {}).get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message is empty")

    values = state.values
    status = values.get("status", "")
    mode = values.get("mode", "")

    # ── Agent dialog mode (non-streaming fallback) ──
    if status == "dialog" and mode == "agent" and not values.get("agent_dialog_complete", False):
        from code_tutor_agent.agents.agent_dialog import analyze_user_intent, build_ready_message

        history = list(values.get("agent_dialog_history", []))
        history.append(Message(role="user", content=message))
        graph.update_state(config, {"agent_dialog_history": history, "tutor_messages": history})

        intent = analyze_user_intent(history)

        if intent.is_ready:
            topic = intent.topic or values.get("topic", "数组")
            difficulty = intent.difficulty or values.get("difficulty", "easy")
            ready_msg = build_ready_message(topic, difficulty)
            history.append(ready_msg)
            graph.update_state(config, {
                "agent_dialog_history": history,
                "agent_dialog_complete": True,
                "topic": topic,
                "difficulty": difficulty,
                "tutor_messages": history,
            })
            import asyncio as _asyncio
            async def _safe_invoke():
                try:
                    cur = graph.get_state(config)
                    await _asyncio.to_thread(graph.invoke, dict(cur.values), config)
                except Exception as e:
                    logger.error("Background graph.invoke failed: %s", e, exc_info=True)
            _asyncio.create_task(_safe_invoke())
            return {"response": intent.next_message or ready_msg.content}
        else:
            history.append(Message(role="tutor", content=intent.next_message))
            graph.update_state(config, {"agent_dialog_history": history, "tutor_messages": history})
            return {"response": intent.next_message}

    # ── Normal tutoring chat (non-streaming) ──
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    llm = get_llm("agnes", temperature=0.7)
    prompt = (
        f"你是一个编程导师。用户正在做一道算法题「{title}」。\n"
        f"用户当前的消息：{message}\n\n"
        f"请给出有帮助的指导和建议，不要直接给出完整代码。回复控制在 200 字以内。"
    )
    try:
        response = llm.invoke(prompt)
        reply = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.warning("Chat LLM failed: %s", exc)
        reply = "抱歉，我现在无法回答。请稍后再试。"

    current_msgs = list(values.get("tutor_messages", []))
    current_msgs.append({"role": "user", "content": message})
    current_msgs.append({"role": "tutor", "content": reply})
    graph.update_state(config, {"tutor_messages": current_msgs})

    return {"response": reply}