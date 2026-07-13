"""Chat router — streaming + non-streaming chat endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from langchain_core.messages import HumanMessage
from starlette.responses import StreamingResponse

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)
router = APIRouter()


def _chunk_text(text: str, size: int = 6):
    """把文本切成小段，用于伪流式输出，保持打字效果。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


@router.post("/{sid}/chat/stream")
async def chat_with_tutor_stream(sid: str, body: dict, background_tasks: BackgroundTasks):
    """Streaming chat with the AI tutor via SSE."""
    from code_tutor_agent.config import get_llm
    from code_tutor_agent.agents.agent_dialog import (
        analyze_user_intent,
        build_ready_message,
        DialogIntent,
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

    # 题目生成后台任务：用 BackgroundTasks 触发，保证 SSE 响应发送后
    # 一定跑完（不受连接关闭影响），写入 problem 后前端轮询自动跳转
    async def _safe_invoke():
        try:
            cur = graph.get_state(config)
            await asyncio.to_thread(graph.invoke, dict(cur.values), config)
        except Exception as e:
            logger.error("Background graph.invoke failed: %s", e, exc_info=True)

    # Agent 对话模式：仅当对话未完成时
    if status == "dialog" and mode == "agent" and not agent_done:
        raw_history = values.get("agent_dialog_history", [])
        # 统一转为 Message 对象（防御性：checkpointer 中可能混入 dict）
        history: list[Message] = []
        for m in raw_history:
            if isinstance(m, Message):
                history.append(m)
            elif isinstance(m, dict):
                history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                history.append(Message(role="tutor", content=str(m)))
        history.append(Message(role="user", content=message))
        graph.update_state(config, {"agent_dialog_history": history, "tutor_messages": history})

        async def dialog_event_stream():
            nonlocal history

            # ── 先判定意图（单次结构化调用），再决定回复内容与路由 ──
            # 把「自然回复」与「is_ready 路由判定」合并为同一次 LLM 判定，
            # 避免两个模型各说各话、互相矛盾（对话衔接修复-2）
            try:
                intent = analyze_user_intent(history)
            except Exception as exc:
                logger.warning("analyze_user_intent failed: %s", exc)
                intent = DialogIntent(
                    next_message="我没理解清楚，能再详细说说你想练什么类型的题吗？"
                )

            if intent.is_ready:
                topic = intent.topic or values.get("topic", "数组")
                difficulty = intent.difficulty or values.get("difficulty", "easy")
                # 收尾回复固定为「正在生成题目」提示，不再让自由模型临场发挥
                # 说出「题目信息遗漏」这类错位文案（对话衔接修复-1）
                ready_msg = build_ready_message(topic, difficulty)
                history.append(ready_msg)
                # 立即置 awaiting_problem，前端可进入「生成中」视图（对话衔接修复-3）
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "agent_dialog_complete": True,
                    "status": "awaiting_problem",
                    "topic": topic,
                    "difficulty": difficulty,
                    "tutor_messages": history,
                })

                # 伪流式输出固定收尾文案，保持打字效果
                for chunk in _chunk_text(ready_msg.content):
                    yield f"data: {chunk}\n\n"

                # 用 BackgroundTasks 可靠触发题目生成（planner→generator），
                # 避免 SSE 连接关闭后 asyncio.create_task 子任务被取消、
                # 导致 problem 永不写入、前端无法自动跳转（自动跳转修复）
                background_tasks.add_task(_safe_invoke)
            else:
                # 非 ready：回复来自同一次判定的 next_message（已合并，无需再调自由模型）
                reply = intent.next_message or "好的，我明白了，能再具体说说吗？"
                history.append(Message(role="tutor", content=reply))
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "tutor_messages": history,
                })
                for chunk in _chunk_text(reply):
                    yield f"data: {chunk}\n\n"

            yield "data: __DONE__\n\n"

        return StreamingResponse(
            dialog_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Normal tutoring chat ──
    # 检查 graph 是否在 interrupt 处暂停（wait_for_submit_node）。
    # 如果是，astream 不可用 — 降级为直接 LLM 流式输出。
    state_snap = graph.get_state(config)
    has_interrupt = bool(state_snap.next)

    if has_interrupt:
        # 直接 LLM 流式输出（graph 在 interrupt 处，无法 astream）
        from code_tutor_agent.config import get_llm
        llm = get_llm("agnes-stream", temperature=0.7)

        # 在 prompt 中反映最近的消息
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

            # 手动保存到 state
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
async def chat_with_tutor(sid: str, body: dict, background_tasks: BackgroundTasks):
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

        raw_history = values.get("agent_dialog_history", [])
        # 统一转为 Message 对象（防御性：checkpointer 中可能混入 dict）
        history: list[Message] = []
        for m in raw_history:
            if isinstance(m, Message):
                history.append(m)
            elif isinstance(m, dict):
                history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                history.append(Message(role="tutor", content=str(m)))
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
            async def _safe_invoke():
                try:
                    cur = graph.get_state(config)
                    await asyncio.to_thread(graph.invoke, dict(cur.values), config)
                except Exception as e:
                    logger.error("Background graph.invoke failed: %s", e, exc_info=True)
            background_tasks.add_task(_safe_invoke)
            return {"response": intent.next_message or ready_msg.content}
        else:
            history.append(Message(role="tutor", content=intent.next_message))
            graph.update_state(config, {"agent_dialog_history": history, "tutor_messages": history})
            return {"response": intent.next_message}

    # ── Normal tutoring chat (non-streaming) ──
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    # ── Reviewing phase: resume graph via tutor_router ──
    if values.get("phase") == "reviewing":
        current_msgs = list(values.get("tutor_messages", []))
        current_msgs.append({"role": "user", "content": message})
        graph.update_state(config, {"tutor_messages": current_msgs})
        await asyncio.to_thread(graph.invoke, None, config)
        final_state = graph.get_state(config)
        final_msgs = final_state.values.get("tutor_messages", [])
        reply = final_msgs[-1]["content"] if final_msgs and isinstance(final_msgs[-1], dict) else (final_msgs[-1].content if final_msgs else "")
        return {"response": reply}

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