"""Session router — create, list, delete session, submit code, by-problem, state, reference."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from langgraph.types import Command

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.api.serializers import serialize_state, empty_state
from code_tutor_agent.api.services.generation import run_generation, run_fast_path
from code_tutor_agent.config import get_checkpoint_db_path
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.api import CreateSessionRequest, NextProblemReq, NextProblemResp, SubmitRequest, SubmitResponse, SessionStateResponse
from code_tutor_agent.schemas.state import SessionState, ProblemMeta, Message

logger = logging.getLogger(__name__)
router = APIRouter()

# ── checkpointer 辅助 ──

CHECKPOINT_DB = get_checkpoint_db_path()


def _checkpointer_conn() -> sqlite3.Connection | None:
    """获取 checkpointer 底层的 SQLite 连接，用于直查/直删。"""
    try:
        graph = get_graph()
        cp = graph.checkpointer
        if hasattr(cp, "conn"):
            return cp.conn
    except RuntimeError:
        pass
    return None


def _session_exists(thread_id: str) -> bool:
    """检查 checkpoints.db 中是否存在该 thread_id。"""
    conn = _checkpointer_conn()
    if not conn:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


@router.post("")
async def create_session(body: CreateSessionRequest | None = None):
    """Create a new tutoring session (background generation)."""
    graph = get_graph()
    sid = str(uuid.uuid4())
    config = {"configurable": {"thread_id": sid}}

    initial_dict = {"session_id": sid}
    if body:
        if body.topic:
            initial_dict["topic"] = body.topic
        if body.difficulty:
            initial_dict["difficulty"] = body.difficulty
        if body.mode:
            initial_dict["mode"] = body.mode
        if body.leetcode:
            initial_dict["leetcode"] = body.leetcode

    # LeetCode 快速路径
    if body and body.leetcode and body.leetcode.get("parsed_test_cases"):
        return run_fast_path(sid, body.model_dump() if hasattr(body, "model_dump") else body, graph, config)

    # 记录活跃时间（TTL 清理用）
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    _generation_progress[sid] = []
    asyncio.create_task(run_generation(sid, initial_dict))
    return {"session_id": sid, "status": "generating"}


# ── 会话列表 / 删除 ──


@router.get("/list")
async def list_sessions(
    limit: int = Query(default=50, ge=1, le=200, description="最多返回条数"),
):
    """列出所有持久化的会话（按最近活跃倒序）。

    返回每个会话的 session_id、状态摘要和最后活跃时间。
    依赖于 checkpoints.db 中的数据。
    """
    conn = _checkpointer_conn()
    if not conn:
        return {"sessions": [], "total": 0}

    try:
        # 每个 thread 取最新一条 checkpoint 的 metadata
        rows = conn.execute(
            """
            SELECT
                thread_id,
                checkpoint,
                metadata,
                MAX(ROWID) AS _rowid
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY _rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        sessions: list[dict[str, Any]] = []
        for row in rows:
            thread_id = row[0]
            checkpoint_blob = row[1]
            metadata_blob = row[2]

            # 解析 checkpoint blob → 提取状态字段
            info = {
                "session_id": thread_id,
                "status": "unknown",
                "topic": "",
                "difficulty": "",
                "mode": "practice",
                "problem_title": "",
                "last_verdict": "",
            }

            try:
                if checkpoint_blob:
                    # LangGraph checkpoint 序列化格式
                    cp = json.loads(checkpoint_blob)
                    ch_values = cp.get("channel_values", {})
                    # channel_values 的值通常是 BLOB（base64 编码的 pickled dict）
                    # 尝试从 channel_values 中提取关键字段
                    for key in ("status", "topic", "difficulty", "mode",
                                "last_verdict", "session_id"):
                        if key in ch_values:
                            info[key] = str(ch_values[key])

                    # 尝试提取 problem title
                    problem = ch_values.get("problem", "")
                    if problem and isinstance(problem, str) and len(problem) > 5:
                        info["problem_title"] = problem[:80]
            except Exception:
                pass

            sessions.append(info)

        return {"sessions": sessions, "total": len(sessions)}

    except Exception as exc:
        logger.exception("list_sessions failed")
        raise HTTPException(500, f"Failed to list sessions: {exc}")


@router.delete("/{sid}")
async def delete_session(sid: str):
    """删除一个会话及其所有 checkpoint 数据。

    清理内容：
    - LangGraph checkpointer 中该 thread_id 的所有 checkpoint
    - 内存中的进度消息（_generation_progress）
    """
    graph = get_graph()

    if not _session_exists(sid):
        raise HTTPException(404, f"Session {sid} not found")

    try:
        checkpointer = graph.checkpointer
        if hasattr(checkpointer, "delete_thread"):
            checkpointer.delete_thread(sid)
            logger.info("Deleted thread %s from checkpointer", sid)

        _generation_progress.pop(sid, None)

        # 清理活跃时间记录
        try:
            from code_tutor_agent.db.database import delete_session_activity
            delete_session_activity(sid)
        except Exception as exc:
            logger.warning("delete_session_activity failed for %s: %s", sid, exc)

        return {"session_id": sid, "deleted": True}
    except Exception as exc:
        logger.exception("Failed to delete session %s", sid)
        raise HTTPException(500, f"Failed to delete session: {exc}")


@router.post("/cleanup")
async def cleanup_sessions(
    max_age_hours: int = Query(default=168, ge=1, description="清理多少小时前的会话（默认 7 天）"),
    dry_run: bool = Query(default=True, description="仅预览，不实际删除"),
):
    """清理过期会话的 checkpoint 数据。

    基于 session_activity 表的 last_active_at 时间戳精确判断过期，
    不再使用 ROWID 近似。
    """
    from code_tutor_agent.db.database import get_stale_sessions, delete_session_activity

    stale = get_stale_sessions(max_age_hours)

    if dry_run:
        return {
            "cleaned": 0,
            "dry_run": True,
            "stale_count": len(stale),
            "stale_sessions": stale[:20],  # 最多预览 20 个
            "message": f"Dry run: 发现 {len(stale)} 个过期会话（>{max_age_hours}h 未活跃）",
        }

    graph = get_graph()
    checkpointer = graph.checkpointer
    cleaned = 0
    errors = 0

    for tid in stale:
        try:
            if hasattr(checkpointer, "delete_thread"):
                checkpointer.delete_thread(tid)
            delete_session_activity(tid)
            _generation_progress.pop(tid, None)
            cleaned += 1
        except Exception as exc:
            logger.warning("Cleanup failed for session %s: %s", tid, exc)
            errors += 1

    logger.info("Cleanup: %d deleted, %d errors, %d total stale", cleaned, errors, len(stale))
    return {"cleaned": cleaned, "errors": errors, "dry_run": False, "stale_count": len(stale)}


@router.post("/{sid}/submit", response_model=SubmitResponse)
async def submit_code(sid: str, body: SubmitRequest):
    """Resume a paused session with user-submitted code."""
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    if state.values.get("status") == "done":
        raise HTTPException(400, "Session is already done")

    # 记录活跃时间
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    logger.info("POST /session/%s/submit → code=%d chars", sid, len(body.code))

    graph.invoke(Command(resume={"code": body.code, "language": body.language}), config)
    state = graph.get_state(config)
    values = state.values

    # 持久化提交记录
    try:
        problem = values.get("problem")
        subs = values.get("submissions", [])
        if subs and problem:
            pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
            last_sub = subs[-1] if isinstance(subs, list) else subs
            from code_tutor_agent.db.database import save_submission
            verdict = getattr(last_sub, "last_verdict", "") or getattr(last_sub, "verdict", "") or values.get("last_verdict", "")
            if verdict:
                raw_results = getattr(last_sub, "judge_results", [])
                serialised = [
                    r.model_dump() if hasattr(r, "model_dump") else r
                    for r in raw_results
                ]
                last_row_id = save_submission(pid, body.code, verdict, serialised, session_id=sid)
                logger.info("saved submission successfully lastrowid,  %s", last_row_id)
    except Exception as exc:
        logger.warning("Failed to persist submission: %s", exc)

    tutor_messages = values.get("tutor_messages", [])
    tutor_msg = None
    if tutor_messages:
        last = tutor_messages[-1]
        tutor_msg = last["content"] if isinstance(last, dict) else last.content

    return SubmitResponse(
        session_id=sid,
        status=values.get("status", "unknown"),
        verdict=values.get("last_verdict"),
        tutor_message=tutor_msg,
        hint_level=values.get("hint_level", 0),
    )


@router.get("/{sid}/state", response_model=SessionStateResponse)
async def get_session_state(sid: str):
    """Poll the current session state.

    如果 session 不存在则返回 404，前端可据此区分"生成中"和"无效会话"。
    """
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

    if not _session_exists(sid):
        raise HTTPException(404, f"Session {sid} not found")

    # 记录活跃时间（TTL 清理用）
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    try:
        state = graph.get_state(config)
    except Exception:
        return empty_state(sid)

    return serialize_state(state.values)


@router.get("/{sid}/reference")
async def get_reference_code(sid: str):
    """Get the reference solution (only after AC)."""
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}
    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    if state.values.get("last_verdict") != "AC":
        raise HTTPException(403, "Reference code is only available after AC")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded")

    from code_tutor_agent.db.database import get_problem_by_id
    pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    full = get_problem_by_id(pid)
    if not full:
        raise HTTPException(500, "Problem not found in DB")
    return {"code": full.optimal_solution or full.brute_solution, "title": full.title}


@router.post("/by-problem/{problem_id}")
async def create_session_with_existing(problem_id: int):
    """Create a session using an existing problem from the database."""
    graph = get_graph()
    from code_tutor_agent.db.database import get_problem_by_id

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    sid = str(uuid.uuid4())
    config = {"configurable": {"thread_id": sid}}

    visible_tcs = full.get("visible_test_cases", [])
    if not visible_tcs:
        raw_test_cases = full.get("test_cases", [])
        visible_tcs = [
            {"input_args": tc.get("input_args", []), "expected_output": tc.get("expected_output", ""), "explanation": tc.get("explanation", "")}
            for tc in raw_test_cases if not tc.get("is_hidden", False)
        ]

    meta = ProblemMeta(
        problem_id=full["id"],
        title=full["title"],
        topic=full.get("topic", ""),
        difficulty=full.get("difficulty", "medium"),
        description=full.get("description", ""),
        starter_code=full.get("starter_code", ""),
        visible_test_cases=visible_tcs,
        novelty_score=full.get("novelty_score", 7.0),
        tag_primary="array_basics",
        prob_elo=1200 if full.get("difficulty", "easy") == "easy" else 1500 if full.get("difficulty") == "medium" else 1800,
    )

    initial_dict = {
        "session_id": sid, "problem": meta, "status": "awaiting_submit",
        "topic": meta.topic, "difficulty": meta.difficulty,
        "submissions": [], "hint_level": 0, "tutor_messages": [],
        "last_verdict": None, "error_message": "",
    }
    initial = SessionState(**initial_dict)
    graph.invoke(initial.model_dump(), config)
    state = graph.get_state(config)
    return serialize_state(state.values)


@router.post("/{sid}/next-problem", response_model=NextProblemResp)
async def next_problem(sid: str, body: NextProblemReq):
    """Continue to the next problem within the same session.

    - Agent mode: re-enter the tutor dialog (preserve history, hide the
      problem).  No new problem is generated until the user and tutor
      agree on a new topic/difficulty in chat (Bug 5/8/9).
    - Other modes: route through critic → planner → generator (existing).
    """
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    # 记录活跃时间
    try:
        from code_tutor_agent.db.database import touch_session
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    vals = state.values
    mode = vals.get("mode", "practice")

    # ── Agent mode: re-enter the tutor dialog with cross-problem summary ──
    #
    # 上下文管理 v2：
    #   - 不再全量保留历史对话（第 N 题时不再包含前 N-1 题的完整 transcript）
    #   - 换题时生成跨题摘要存入 context_summary
    #   - 同时构建下一题引导消息，重置对话到初始状态
    #
    # 效果：第 3 题时 LLM 看到的上下文 = 跨题摘要(~300 token) + 当前题对话(~3000 token)
    #      而不是旧方案的 全量历史(~15000+ token) + 当前题对话
    if mode == "agent":
        from code_tutor_agent.context_manager import build_cross_problem_context
        from code_tutor_agent.progress import _generation_progress

        _generation_progress[sid] = ["回到与导师的对话…"]

        # ── 1. 构建跨题上下文摘要 ──
        # 从 problem_history 中提取每道题的结构化结果，不保留对话原文
        problem_history = vals.get("problem_history") or []
        cross_context = build_cross_problem_context(problem_history)

        # ── 2. 生成本题对话摘要（如果有对话内容） ──
        _full_history = vals.get("tutor_messages") or vals.get("agent_dialog_history") or []
        # 统一转为 Message 对象，避免混入 dict 导致后续处理报错
        _norm_history: list[Message] = []
        for m in _full_history:
            if isinstance(m, Message):
                _norm_history.append(m)
            elif isinstance(m, dict):
                _norm_history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _norm_history.append(Message(role="tutor", content=str(m)))

        # 异步生成本题对话摘要
        dialogue_summary = ""
        if problem_history:
            try:
                from code_tutor_agent.context_manager import generate_summary
                last_record = problem_history[-1] if problem_history else None
                dialogue_summary = generate_summary(_norm_history, last_record)
            except Exception as exc:
                logger.warning("Failed to generate dialogue summary: %s", exc)

        # ── 3. 合并跨题上下文 + 本提摘要 → context_summary ──
        if dialogue_summary:
            new_summary = f"{cross_context}\n\n## 上一题对话要点\n{dialogue_summary}" if cross_context else dialogue_summary
        else:
            new_summary = cross_context or None

        # ── 4. 构造"下一题"引导消息，重置对话 ──
        # 根据上一题的 verdict 选择不同措辞
        prev_verdict = vals.get("last_verdict")
        if prev_verdict == "AC":
            _guide_content = (
                "上一题完美拿下！接下来想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？"
            )
        elif prev_verdict == "WA":
            _guide_content = (
                "这道题还差一点，不过没关系，换个方向转换一下思路。"
                "接下来想练什么类型？数组、链表、双指针、动态规划都可以~"
            )
        else:
            # 用户未提交就放弃，或没有 verdict
            _guide_content = (
                "好的，这道题先放一放。接下来想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？"
            )
        guide_msg = Message(role="tutor", content=_guide_content)

        # tutor_messages 不清空：保留完整对话历史供前端展示，
        # 追加下一题引导消息。LLM 上下文由 agent_dialog_history（当前题）+ context_summary（跨题摘要）承载。
        _existing_tutor = vals.get("tutor_messages") or []
        _display_history = list(_existing_tutor) + [guide_msg]

        graph.update_state(config, {
            "status": "dialog",
            "problem": None,
            "agent_dialog_complete": False,
            "agent_dialog_history": [guide_msg],
            "tutor_messages": _display_history,
            "context_summary": new_summary,
            "topic": "",
            "difficulty": "",
            "phase": "dialog",
            "last_verdict": None,
            "judge_report": None,
            "hint_level": 0,
            "pending_abandon": False,
            "next_preference": None,
        }, as_node="agent_dialog_node")

        # invoke(None) 在这里是空操作，但保留以维持 checkpointer 一致性
        await asyncio.to_thread(graph.invoke, None, config)

        new_state = graph.get_state(config)
        new_vals = new_state.values
        # 返回完整连续对话（含出题前 + 做题中 + 反馈 + 下一题引导）
        history = new_vals.get("tutor_messages") or new_vals.get("agent_dialog_history") or []

        def _ser(m):
            return m.model_dump() if hasattr(m, "model_dump") else (
                m if isinstance(m, dict) else {"role": "tutor", "content": str(m)}
            )

        logger.info("Agent re-enter dialog — history=%d", len(history))
        return NextProblemResp(
            session_id=sid,
            problem=None,
            phase="dialog",
            hint_level=0,
            tutor_messages=[_ser(m) for m in history],
        )

    # ── Normal modes: critic flush → planner → generator (existing flow) ──
    # 1. Check if current problem has a terminal verdict
    has_terminal = (
        vals.get("last_verdict") in ("AC", "WA")
        and vals.get("judge_report") is not None
    )
    need_abandon = not has_terminal and vals.get("phase") in ("solving", "reviewing")

    # 2. Set up progress messages (frontend polls /state during generation)
    from code_tutor_agent.progress import _generation_progress
    _generation_progress[sid] = ["正在准备下一题…"]

    # 3. Patch trigger flags (as_node routes next invoke into critic_node)
    graph.update_state(config, {
        "pending_abandon": need_abandon,
        "next_preference": body.preference,
    }, as_node="critic_node")

    # 4. Invoke — generator self-verify is sync, run in threadpool
    await asyncio.to_thread(graph.invoke, None, config)

    # 5. Read new state
    new_state = graph.get_state(config)
    new_vals = new_state.values

    problem = new_vals.get("problem")
    if not problem:
        raise HTTPException(500, "Next problem generation failed")

    return NextProblemResp(
        session_id=sid,
        problem=problem.model_dump() if hasattr(problem, "model_dump") else problem,
        phase=new_vals.get("phase", "solving"),
        hint_level=0,
    )