"""Chat router — streaming + non-streaming chat endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
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

    # 记录活跃时间（TTL 清理用）
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

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

        # 构建完整前端展示历史（保留跨题对话，不清空）
        _full_display: list[Message] = []
        for m in (values.get("tutor_messages") or []):
            if isinstance(m, Message):
                _full_display.append(m)
            elif isinstance(m, dict):
                _full_display.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _full_display.append(Message(role="tutor", content=str(m)))
        _full_display.append(Message(role="user", content=message))

        graph.update_state(config, {
            "agent_dialog_history": history,
            "tutor_messages": _full_display,
        })

        # 提取跨题摘要（Agent 模式换题时生成），注入到意图分析中
        context_summary = values.get("context_summary")
        if context_summary:
            logger.info("Agent dialog enriched with context_summary: %d chars", len(str(context_summary)))

        async def dialog_event_stream():
            nonlocal history, context_summary

            # ── 先判定意图（单次结构化调用），再决定回复内容与路由 ──
            # 把「自然回复」与「is_ready 路由判定」合并为同一次 LLM 判定，
            # 避免两个模型各说各话、互相矛盾（对话衔接修复-2）
            try:
                intent = analyze_user_intent(history, context_summary=context_summary)
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
                _full_display.append(ready_msg)
                # 立即置 awaiting_problem，前端可进入「生成中」视图（对话衔接修复-3）
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "agent_dialog_complete": True,
                    "status": "awaiting_problem",
                    "topic": topic,
                    "difficulty": difficulty,
                    "tutor_messages": _full_display,
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
                _full_display.append(Message(role="tutor", content=reply))
                graph.update_state(config, {
                    "agent_dialog_history": history,
                    "tutor_messages": _full_display,
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
    # 所有非 agent-dialog 聊天统一走直接 LLM 流式输出。
    # 原因：graph 可能在 interrupt 处（SqliteSaver 不支持 astream）
    #       或 graph 停在 END（如 AC 后 phase=reviewing），
    #       graph.stream(None, config) 从 END 不会重启，返回空。
    # 统一走直接 LLM 是最可靠的做法。
    from code_tutor_agent.config import get_llm
    llm = get_llm("agnes-stream", temperature=0.7)

    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    # 收集 tutor_messages 中近期对话作为上下文
    _tutor_msgs = values.get("tutor_messages") or []
    _context_lines = []
    for msg in _tutor_msgs[-8:]:  # 只取最近 8 条
        role = msg.get("role", "tutor") if isinstance(msg, dict) else getattr(msg, "role", "tutor")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if content:
            label = "导师" if role == "tutor" else "用户"
            _context_lines.append(f"{label}: {content}")
    _chat_context = "\n".join(_context_lines) if _context_lines else "（无）"

    # 根据 phase 匹配合适的 system prompt
    _phase = values.get("phase", "")
    _verdict = values.get("last_verdict", "")

    if _phase == "reviewing":
        # AC 后复盘/讨论模式
        system = (
            "你是 AI 编程导师，用户刚通过了一道算法题（AC），"
            "现在在回顾和讨论这道题。你的任务是：\n"
            "- 解释解题思路、时间/空间复杂度\n"
            "- 讨论其他可能的解法（暴力/优化/不同数据结构）\n"
            "- 分析这道题的易错点和面试常见追问\n"
            "- 如果用户要求出下一题，引导他说出想练的方向\n"
            "语气鼓励、专业，回复控制在 300 字以内。"
        )
    elif _verdict == "WA":
        # 刚提交 WA，正在辅导中
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户刚提交的代码没有通过（WA），"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )
    else:
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户正在做题，"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )

    async def normal_chat_stream():
        user_prompt = (
            f"算法题：{title}\n\n"
            f"近期对话：\n{_chat_context}\n\n"
            f"用户当前消息：{message}"
        )
        full = []
        try:
            async for chunk in llm.astream([
                ("system", system),
                ("human", user_prompt),
            ]):
                token = chunk.content if hasattr(chunk, "content") else str(chunk)
                if token:
                    full.append(token)
                    yield f"data: {token}\n\n"
        except Exception as exc:
            logger.warning("Normal chat LLM failed: %s", exc)

        reply = "".join(full)
        if not reply.strip():
            reply = "抱歉，我现在无法回答。请稍后再试。"
        if not full:
            yield f"data: {reply}\n\n"

        # 手动保存到 state
        tutor_msgs = list(values.get("tutor_messages", []))
        tutor_msgs.append({"role": "user", "content": message})
        tutor_msgs.append({"role": "tutor", "content": reply})
        try:
            graph.update_state(config, {"tutor_messages": tutor_msgs})
        except Exception as exc:
            logger.warning("Failed to save chat: %s", exc)
        yield "data: __DONE__\n\n"

    return StreamingResponse(normal_chat_stream(), media_type="text/event-stream")


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

    # 记录活跃时间（TTL 清理用）
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

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

        # 构建完整前端展示历史（保留跨题对话，不清空）
        _full_display: list[Message] = []
        for m in (values.get("tutor_messages") or []):
            if isinstance(m, Message):
                _full_display.append(m)
            elif isinstance(m, dict):
                _full_display.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _full_display.append(Message(role="tutor", content=str(m)))
        _full_display.append(Message(role="user", content=message))

        graph.update_state(config, {
            "agent_dialog_history": history,
            "tutor_messages": _full_display,
        })

        context_summary = values.get("context_summary")
        intent = analyze_user_intent(history, context_summary=context_summary)

        if intent.is_ready:
            topic = intent.topic or values.get("topic", "数组")
            difficulty = intent.difficulty or values.get("difficulty", "easy")
            ready_msg = build_ready_message(topic, difficulty)
            history.append(ready_msg)
            _full_display.append(ready_msg)
            graph.update_state(config, {
                "agent_dialog_history": history,
                "agent_dialog_complete": True,
                "topic": topic,
                "difficulty": difficulty,
                "tutor_messages": _full_display,
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
            _full_display.append(Message(role="tutor", content=intent.next_message))
            graph.update_state(config, {
                "agent_dialog_history": history,
                "tutor_messages": _full_display,
            })
            return {"response": intent.next_message}

    # ── Normal tutoring chat (non-streaming) ──
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    # ── Normal tutoring chat (non-streaming): 统一走直接 LLM ──
    # 原因同流式路径：graph 可能在 interrupt 或 END，无法可靠地通过 graph.invoke 走 chat_node
    llm = get_llm("agnes", temperature=0.7)

    _phase = values.get("phase", "")
    _verdict = values.get("last_verdict", "")

    if _phase == "reviewing":
        system = (
            "你是 AI 编程导师，用户刚通过了一道算法题（AC），"
            "现在在回顾和讨论这道题。你的任务是：\n"
            "- 解释解题思路、时间/空间复杂度\n"
            "- 讨论其他可能的解法\n"
            "- 分析易错点和面试常见追问\n"
            "语气鼓励、专业，回复控制在 300 字以内。"
        )
    elif _verdict == "WA":
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户刚提交的代码没有通过（WA），"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )
    else:
        system = (
            "你是 AI 编程导师，语气温暖鼓励。用户正在做题，"
            "根据对话上下文分析问题、给出针对性建议。"
            "不要直接给出完整代码，引导用户自己思考。回复控制在 200 字以内。"
        )

    user_prompt = (
        f"算法题：{title}\n\n"
        f"用户当前消息：{message}"
    )
    try:
        response = llm.invoke([
            ("system", system),
            ("human", user_prompt),
        ])
        reply = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.warning("Chat LLM failed: %s", exc)
        reply = "抱歉，我现在无法回答。请稍后再试。"

    current_msgs = list(values.get("tutor_messages", []))
    current_msgs.append({"role": "user", "content": message})
    current_msgs.append({"role": "tutor", "content": reply})
    graph.update_state(config, {"tutor_messages": current_msgs})

    return {"response": reply}