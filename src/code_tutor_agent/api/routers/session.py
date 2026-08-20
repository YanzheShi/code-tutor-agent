"""Session router — create, list, delete session, submit code, by-problem, state, reference."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse
from langchain_core.tracers.context import collect_runs
from langgraph.types import Command
from pydantic import BaseModel, Field

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.api.serializers import empty_state, serialize_state
from code_tutor_agent.api.services.generation import GENERATION_TIMEOUT, run_generation
from code_tutor_agent.config import get_checkpoint_db_path
from code_tutor_agent.context_manager import build_cross_problem_context, generate_summary
from code_tutor_agent.db.database import (
    delete_session_activity,
    delete_session_sidecar_data,
    get_analysis_result,
    get_problem_by_id,
    get_stale_sessions,
    get_trace_analysis,
    get_trace_summary,
    save_edit_trace,
    save_submission,
    touch_session,
)
from code_tutor_agent.observability import build_run_config, record_verdict_feedback
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.api import (
    CreateSessionRequest,
    NextProblemReq,
    NextProblemResp,
    SessionStateResponse,
    SubmitRequest,
    SubmitResponse,
)
from code_tutor_agent.schemas.state import Message, ProblemMeta, SessionState
from code_tutor_agent.trace import (
    archive_thread,
    continue_analysis,
    first_round_analysis,
    list_thread_for_display,
    summarize_thread,
)

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
async def create_session(background_tasks: BackgroundTasks, body: CreateSessionRequest | None = None):
    """Create a new tutoring session (background generation)."""
    graph = get_graph()
    sid = str(uuid.uuid4())
    config = build_run_config(
        sid,
        mode="agent",  # normal 模式已删除，统一 agent
        topic=body.topic if body else None,
        difficulty=body.difficulty if body else None,
        run_name="create_session",
    )

    initial_dict = {"session_id": sid}
    if body:
        if body.topic:
            initial_dict["topic"] = body.topic
        if body.difficulty:
            initial_dict["difficulty"] = body.difficulty
        # normal 模式已删除，统一 agent（忽略前端传入的 mode）
        initial_dict["mode"] = "agent"
        if body.leetcode_url:
            # 仅传 URL，解析收口到 generator_node（与 agent 对话路径统一）
            initial_dict["leetcode"] = {"url": body.leetcode_url}

    # 记录活跃时间（TTL 清理用）
    try:
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    _generation_progress[sid] = []
    # 用 FastAPI BackgroundTasks 跑完整“出题 + 标注/生成完整用例”，响应返回后才执行、
    # 客户端断开也不会被取消（比 asyncio.create_task 更稳）；立即返回 session_id，
    # 前端进入 loading 用 SSE 收进度。
    background_tasks.add_task(run_generation, sid, initial_dict)
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
            delete_session_activity(sid)
        except Exception as exc:
            logger.warning("delete_session_activity failed for %s: %s", sid, exc)

        # 清理旁路数据（edit_traces / analysis_results / trace_summaries / trace_threads / submissions）
        delete_session_sidecar_data(sid)

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
            delete_session_sidecar_data(tid)
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
    config = build_run_config(sid, run_name="submit_code")

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")
    # 富化 metadata（topic/difficulty/mode/problem_id）供 LangSmith 按会话筛查
    config = build_run_config(
        sid,
        mode=state.values.get("mode"),
        topic=state.values.get("topic"),
        difficulty=state.values.get("difficulty"),
        problem_id=state.values.get("problem_id"),
        run_name="submit_code",
    )

    # AC 后 critic_node 会让 graph 重新暂停在 wait_for_submit_node（interrupt），
    # 此时 status 仍为 "done"，但存在待执行的 wait_for_submit_node，
    # 应允许通过 Command(resume) 续跑判题（前端「继续提交不同解法」）。
    # 仅当 graph 真正终止（无待执行节点）时才拒绝提交。
    _next = list(state.next or [])
    # ── dialog 兜底：旧会话卡在 dialog+problem（graph 停在 END、无挂起中断）──
    # 此时 Command(resume=...) 会空转（无 interrupt 可恢复），提交永远只回开场白。
    # 修复：先置 agent_dialog_complete 并用 invoke(None) 重跑 graph，
    # 经 agent_dialog_node(complete) → planner_node(problem 已加载) 真正暂停到
    # wait_for_submit_node 的 interrupt，再走下方正常 resume 判题。
    if (
        state.values.get("status") == "dialog"
        and state.values.get("problem")
        and "wait_for_submit_node" not in _next
    ):
        logger.warning(
            "submit: session %s stuck in dialog with problem — forwarding to wait_for_submit",
            sid,
        )
        graph.update_state(config, {"agent_dialog_complete": True}, as_node="agent_dialog_node")
        # 注意：invoke(None) 在 checkpoint next=()（无挂起任务）时是空操作，
        # 不会重跑 graph——必须带输入重跑。输入来自旧 checkpoint 会覆盖
        # update_state 的值，所以 complete 要直接写进输入，才会从 __start__ 经
        # agent_dialog_node(complete) → planner_node(problem 已加载) → wait_for_submit 暂停。
        _resume_input = SessionState(**state.values).model_dump()
        _resume_input["agent_dialog_complete"] = True
        await asyncio.to_thread(graph.invoke, _resume_input, config)
        state = graph.get_state(config)
        _next = list(state.next or [])
        if "wait_for_submit_node" not in _next:
            raise HTTPException(409, "会话状态异常，无法判题，请重新开始会话")
    if state.values.get("status") == "done" and "wait_for_submit_node" not in _next:
        raise HTTPException(400, "Session is already done")

    # 记录活跃时间
    try:
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    logger.info("POST /session/%s/submit → code=%d chars", sid, len(body.code))

    def _do_judge() -> str | None:
        # 判题是同步阻塞的（graph.invoke 内含 LLM 调用），丢进线程池执行，
        # 避免独占事件循环、拖垮同进程的其它请求与 SSE 推流。
        with collect_runs() as runs:
            graph.invoke(
                Command(resume={"code": body.code, "language": body.language, "scope": "full"}),
                config,
            )
            # collect_runs() 返回 RunCollectorCallbackHandler，需用 .traced_runs 取 run 列表
            traced = runs.traced_runs if runs else []
            return traced[-1].id if traced else None

    run_id = await asyncio.to_thread(_do_judge)
    state = graph.get_state(config)
    values = state.values

    # 判题 verdict 作为 feedback 回传 LangSmith（record_verdict_feedback 内部非致命）
    if run_id and values.get("last_verdict"):
        record_verdict_feedback(
            run_id,
            values["last_verdict"],
            session_id=sid,
            hint_level=values.get("hint_level", 0),
            judge_cycle=len(values.get("submissions", []) or []),
        )

    # 持久化提交记录
    try:
        problem = values.get("problem")
        subs = values.get("submissions", [])
        if subs and problem:
            pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
            last_sub = subs[-1] if isinstance(subs, list) else subs
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


class EditTraceRequest(BaseModel):
    """前端实时采集的编辑轨迹事件批量上传体。"""
    events: list[dict] = Field(default_factory=list, description="编辑轨迹事件列表(edit/idle/run/submit)")
    problem_id: Optional[str] = None  # 前端当前题；逐事件携带时优先用事件内 problem_id


@router.post("/{sid}/edit-trace")
async def save_edit_trace_endpoint(sid: str, body: EditTraceRequest):
    """接收前端实时采集的编辑轨迹事件（UPSERT 累加），供轨迹分析按题隔离读取。

    仅落库，不在此触发 LLM 分析。每个事件内部写入 problem_id（events_json 聚合存储，
    不另加表列）：前端逐事件携带则保留，缺失用请求体 problem_id 兜底（回退 "default"），
    避免换题瞬间串题。
    """
    if not body.events:
        return {"ok": True, "session_id": sid, "events": 0}
    try:
        pid = body.problem_id or "default"
        events: list[dict] = []
        for e in body.events:
            if not isinstance(e, dict):
                continue
            if not e.get("problem_id"):
                e = {**e, "problem_id": pid}
            events.append(e)
        save_edit_trace(sid, "default", events, problem_id=pid)
    except Exception as exc:
        logger.error("save_edit_trace endpoint failed for %s: %s", sid, exc)
        raise HTTPException(500, "failed to persist edit trace")
    return {"ok": True, "session_id": sid, "events": len(events)}


# ── 独立轨迹分析（纯展示，不回灌画像；用户 AC 后手动触发）──


class AnalyzeRequest(BaseModel):
    """轨迹分析请求体。无 message = 首轮结构化分析；有 message = 多轮继续。"""

    problem_id: str = "default"
    message: Optional[str] = None


@router.post("/{sid}/analyze")
async def analyze_trace_endpoint(sid: str, body: Optional[AnalyzeRequest] = None):
    """触发/继续一次独立的做题轨迹分析（按题隔离、独立线程、不回灌画像）。

    - 无 message：首轮结构化分析（读按题过滤的 edit_traces + 题目完整描述 + 终码）。
    - 有 message：在同题分析线程追加追问，返回自由文本回复。
    body 可选：旧前端无 body 调用（problem_id="default" 退化为全量事件分析）仍可工作。
    """
    body = body or AnalyzeRequest()
    # 从会话状态取当前题 ProblemMeta（完整描述 + 约束 + 示例），供分析 LLM 使用
    problem_meta = None
    try:
        graph = get_graph()
        st = graph.get_state(build_run_config(sid, run_name="analyze"))
        problem_meta = st.values.get("problem")
    except Exception:
        problem_meta = None
    try:
        if body.message:
            reply = continue_analysis(sid, body.problem_id, body.message)
            return {"ok": True, "session_id": sid, "problem_id": body.problem_id, "reply": reply}
        result = first_round_analysis(sid, body.problem_id, problem_meta=problem_meta)
        return {
            "ok": True,
            "session_id": sid,
            "problem_id": body.problem_id,
            "analysis": result.model_dump(),
        }
    except Exception as exc:
        logger.error("analyze_trace_endpoint failed for %s: %s", sid, exc)
        raise HTTPException(500, "trace analysis failed")


@router.get("/{sid}/analysis")
async def get_trace_analysis_endpoint(sid: str, problem_id: str = "default"):
    """读取某题已缓存的轨迹分析首轮结论 + 多轮追问线程（无则 analysis=null）。

    前端刷新后据此恢复「轨迹分析」Tab（首轮结论 + 追问历史）。
    """
    data = (
        get_analysis_result(sid, problem_id)
        if problem_id and problem_id != "default"
        else get_trace_analysis(sid)
    )
    messages = (
        list_thread_for_display(sid, problem_id)
        if problem_id and problem_id != "default"
        else []
    )
    return {
        "session_id": sid,
        "problem_id": problem_id,
        "analysis": data,
        "messages": messages,
    }


class AnalyzeSummarizeRequest(BaseModel):
    """过渡压缩请求体。"""

    problem_id: str = "default"
    transition_action: str = "continue"  # continue | next | change | abandon


@router.post("/{sid}/analyze/summarize")
async def summarize_trace_endpoint(sid: str, body: AnalyzeSummarizeRequest):
    """过渡时把当前题分析线程压缩成摘要（双落点源），并归档线程。

    返回 TraceSummary；前端据此渲染可见卡，并可注入下一题导师上下文（trajectory_summary）。
    """
    try:
        summary = summarize_thread(sid, body.problem_id, body.transition_action)
        archive_thread(sid, body.problem_id)
        return {
            "ok": True,
            "session_id": sid,
            "problem_id": body.problem_id,
            "transition_action": body.transition_action,
            "summary": summary.model_dump(),
        }
    except Exception as exc:
        logger.error("summarize_trace_endpoint failed for %s: %s", sid, exc)
        raise HTTPException(500, "trace summarize failed")


@router.get("/{sid}/state", response_model=SessionStateResponse)
async def get_session_state(sid: str):
    """Poll the current session state.

    如果 session 不存在则返回 404，前端可据此区分"生成中"和"无效会话"。
    """
    graph = get_graph()
    config = build_run_config(sid, run_name="get_session_state")

    if not _session_exists(sid):
        raise HTTPException(404, f"Session {sid} not found")

    # 记录活跃时间（TTL 清理用）
    try:
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    try:
        state = graph.get_state(config)
    except Exception:
        return empty_state(sid)

    return serialize_state(state.values)


@router.get("/{sid}/progress/stream")
async def stream_progress(sid: str):
    """SSE 端点：实时推送出题进度，替代前端 setInterval 轮询。

    前端用 EventSource 订阅本端点：
      - 每出现新进度消息，推送 `event: progress`（`data: {"message": "..."}`）；
      - 题目就绪（problem 非空且状态非 dialog）后，推送最终
        `event: done`（`data: <serialize_state 结果>`）并关闭连接；
      - 生成失败（无题目且出现终态错误标记，或超时），推送 `event: error` 并关闭。
    """
    graph = get_graph()
    config = build_run_config(sid, run_name="stream_progress")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GENERATION_TIMEOUT + 30

    # 终态错误标记：出现这些字样说明降级也已失败，可安全判定为生成失败。
    # 注意「LLM 生成失败…正在从备用题库选题…」是过渡消息，紧接着 _fallback_static_problem
    # 很可能成功，不能据此误报错误（否则会把本可成功的题目判成失败）。
    TERMINAL_ERROR_MARKERS = ("请稍后重试", "请联系老师")

    async def event_gen():
        # 初始时记录已有消息数量，不推送初始快照，只靠轮询收新消息。
        # 初始出题时 _generation_progress 为空，last_idx=0 正常收新消息；
        # 继续出题时 _generation_progress 有旧消息，last_idx 跳过旧消息。
        # 如果进度被重置（如 /next-problem 清空后写入更短的列表），
        # len(msgs) < last_idx 会触发 last_idx=0 重新开始收。
        last_idx = len(_generation_progress.get(sid, []))

        while True:
            msgs = list(_generation_progress.get(sid, []))
            # 检测进度被重置（/next-problem 清空后写入更短列表）
            if len(msgs) < last_idx:
                last_idx = 0
            if len(msgs) > last_idx:
                for m in msgs[last_idx:]:
                    yield f"event: progress\ndata: {json.dumps({'message': m}, ensure_ascii=False)}\n\n"
                last_idx = len(msgs)

            # 检测完成 / 失败
            state = None
            try:
                if _session_exists(sid):
                    st = graph.get_state(config)
                    state = st.values
            except Exception:
                state = None

            status = (state or {}).get("status")
            mode = (state or {}).get("mode")
            problem = (state or {}).get("problem")
            tutor_msgs = (state or {}).get("tutor_messages") or (state or {}).get("agent_dialog_history")
            if problem and status != "dialog":
                # 微小延迟，让 React 先处理完 progress 事件的 re-render，再收 done
                await asyncio.sleep(0.05)
                yield f"event: done\ndata: {json.dumps(serialize_state(state), ensure_ascii=False)}\n\n"
                return
            # Agent 模式：对话阶段（status=dialog 且已有对话内容）即视为“就绪”，
            # 推送 done 让前端进入对话界面，从而去掉前端对 getState 的轮询
            # （dialog 阶段后端不会生成题目，没有题目可等）。
            if mode == "agent" and status == "dialog" and tutor_msgs:
                yield f"event: done\ndata: {json.dumps(serialize_state(state), ensure_ascii=False)}\n\n"
                return
            # 仅当无任何题目、且出现“终态错误”消息（降级也失败）时才报错。
            if not problem and any(
                any(mk in m for mk in TERMINAL_ERROR_MARKERS) for m in msgs
            ):
                yield f"event: error\ndata: {json.dumps({'message': '\u751f\u6210\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5'}, ensure_ascii=False)}\n\n"
                return

            # 生成彻底失败（后端已置 status=error，无题目）：立即报错，不空等超时。
            # 练习/普通模式走此路径（agent 模式失败会回 dialog 态，由上面 done 分支处理）。
            if not problem and status == "error":
                _emsg = (state or {}).get("error_message") or "\u51fa\u9898\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5"
                yield f"event: error\ndata: {json.dumps({'message': _emsg}, ensure_ascii=False)}\n\n"
                return

            if loop.time() > deadline:
                if problem:
                    await asyncio.sleep(0.05)
                    yield f"event: done\ndata: {json.dumps(serialize_state(state), ensure_ascii=False)}\n\n"
                else:
                    yield f"event: error\ndata: {json.dumps({'message': '\u751f\u6210\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5'}, ensure_ascii=False)}\n\n"
                return

            await asyncio.sleep(0.4)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/{sid}/reference")
async def get_reference_code(sid: str):
    """Get the reference solution (only after AC)."""
    graph = get_graph()
    config = build_run_config(sid, run_name="get_reference_code")
    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    if state.values.get("last_verdict") != "AC":
        raise HTTPException(403, "Reference code is only available after AC")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded")

    pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    full = get_problem_by_id(pid)
    if not full:
        raise HTTPException(500, "Problem not found in DB")
    return {"code": full.optimal_solution or full.brute_solution, "title": full.title}


@router.post("/by-problem/{problem_id}")
async def create_session_with_existing(problem_id: int):
    """Create a session using an existing problem from the database."""
    graph = get_graph()

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    sid = str(uuid.uuid4())
    config = build_run_config(
        sid,
        mode="agent",  # 强制 agent，normal 模式已删除
        topic=full.get("topic"),
        difficulty=full.get("difficulty"),
        problem_id=full.get("id"),
        run_name="by_problem",
    )

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
        constraints=full.constraints or [],
        novelty_score=full.get("novelty_score", 7.0),
        tag_primary="array_basics",
        prob_elo=1200 if full.get("difficulty", "easy") == "easy" else 1500 if full.get("difficulty") == "medium" else 1800,
    )

    initial_dict = {
        "session_id": sid, "problem": meta, "status": "awaiting_submit",
        "mode": "agent", "topic": meta.topic, "difficulty": meta.difficulty,
        "submissions": [], "hint_level": 0, "tutor_messages": [],
        "last_verdict": None, "error_message": "",
        # 绕过 agent 对话：题目已就绪，graph 应从 __start__ 直接经
        # agent_dialog_node(complete) → planner_node(problem 已加载) 走到
        # wait_for_submit_node 的 interrupt 暂停，否则会话停在 dialog 状态，
        # 提交/运行会因没有挂起中断而空转（历史缺陷，见修复记录）。
        "agent_dialog_complete": True,
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
    config = build_run_config(sid, run_name="next_problem")

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    # 记录活跃时间
    try:
        touch_session(sid)
    except Exception as exc:
        logger.warning("touch_session failed for %s: %s", sid, exc)

    vals = state.values
    mode = vals.get("mode", "practice")

    # 富化 metadata（topic/difficulty/mode/problem_id）供 LangSmith 按会话筛查
    config = build_run_config(
        sid,
        mode=vals.get("mode"),
        topic=vals.get("topic"),
        difficulty=vals.get("difficulty"),
        problem_id=vals.get("problem_id"),
        run_name="next_problem",
    )

    # ── Agent mode (or "continue generating" after AC): re-enter the tutor dialog ──
    #
    # 上下文管理 v2：
    #   - 不再全量保留历史对话（第 N 题时不再包含前 N-1 题的完整 transcript）
    #   - 换题时生成跨题摘要存入 context_summary
    #   - 同时构建下一题引导消息，重置对话到初始状态
    #
    # "continue_dialog" 语义（2026-07-22）：AC 后用户点"继续出题"，只回到出题对话、
    # 给出出题提示，而不是直接生成下一道题。practice 模式也走这条路径并切到 agent 模式。
    #
    # 效果：第 3 题时 LLM 看到的上下文 = 跨题摘要(~300 token) + 当前题对话(~3000 token)
    #      而不是旧方案的 全量历史(~15000+ token) + 当前题对话
    if mode == "agent" or body.preference == "continue_dialog":
        _generation_progress[sid] = ["回到与导师的对话…"]

        # ── 1. 构建跨题上下文摘要 ──
        # 从 problem_history 中提取每道题的结构化结果，不保留对话原文
        problem_history = vals.get("problem_history") or []
        cross_context = build_cross_problem_context(problem_history)

        # ── 2. 生成本题对话摘要（如果有对话内容） ──
        # 只压缩「自上次换题以来的增量」（tutor_messages_cutoff 起始），
        # 避免每次换题都吃全量历史对话（N 题累计 O(N²) → O(N)）。
        # 旧会话无 cutoff → 0，退化为全量历史（一次性成本，可接受）。
        _cutoff = vals.get("tutor_messages_cutoff") or 0
        _full_history = vals.get("tutor_messages") or []
        _delta_history = (
            list(_full_history)[_cutoff:]
            if _full_history
            else (vals.get("agent_dialog_history") or [])
        )
        # 统一转为 Message 对象，避免混入 dict 导致后续处理报错
        _norm_history: list[Message] = []
        for m in _delta_history:
            if isinstance(m, Message):
                _norm_history.append(m)
            elif isinstance(m, dict):
                _norm_history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _norm_history.append(Message(role="tutor", content=str(m)))

        # 异步生成本题对话摘要
        dialogue_summary = ""
        if _norm_history and problem_history:
            try:
                last_record = problem_history[-1] if problem_history else None
                dialogue_summary = generate_summary(_norm_history, last_record)
            except Exception as exc:
                logger.warning("Failed to generate dialogue summary: %s", exc)

        # ── 3. 合并跨题上下文 + 本提摘要 → context_summary ──
        if dialogue_summary:
            new_summary = f"{cross_context}\n\n## 上一题对话要点\n{dialogue_summary}" if cross_context else dialogue_summary
        else:
            new_summary = cross_context or None

        # ── 3.5 追加上一题轨迹分析摘要（双落点：让下一题导师感知薄弱点）──
        # 过渡时前端已 POST /analyze/summarize 把上一题分析线程压成 TraceSummary 落库，
        # 此处读取并追加专属段落到 context_summary（与 build_cross_problem_context 复用同管线）。
        # 注意：只作导师侧只读上下文，不回灌 profile / memory；线程 transcript 已被归档。
        # SessionState 无顶层 problem_id 字段，取当前 problem（update_state 前仍是旧题）的 id。
        try:
            _prev_prob = vals.get("problem")
            prev_pid = getattr(_prev_prob, "problem_id", None) if _prev_prob else None
            if prev_pid is not None:
                ts = get_trace_summary(sid, str(prev_pid))
                if ts:
                    st_text = ts.get("summary_text") or ""
                    st_bullets = ts.get("bullets") or []
                    if st_text or st_bullets:
                        seg = ["## 上一题轨迹分析摘要（仅供导师参考，勿直接复述给用户）"]
                        if st_text:
                            seg.append(st_text)
                        if st_bullets:
                            seg.append("要点：" + " ｜ ".join(str(b) for b in st_bullets))
                        trace_seg = "\n".join(seg)
                        new_summary = (new_summary + "\n\n" + trace_seg) if new_summary else trace_seg
        except Exception as exc:
            logger.warning("failed to load trace summary for next-problem: %s", exc)

        # ── 4. 构造"下一题"引导消息，重置对话 ──
        # 根据上一题的 verdict 选择不同措辞
        prev_verdict = vals.get("last_verdict")
        if prev_verdict == "AC":
            _guide_content = (
                "上一题完美拿下！接下来想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？\n\n"
                "也可以直接把一道 LeetCode 题目链接发给我，我们接着练 👇"
            )
        elif prev_verdict == "WA":
            _guide_content = (
                "这道题还差一点，不过没关系，换个方向转换一下思路。"
                "接下来想练什么类型？数组、链表、双指针、动态规划都可以~\n\n"
                "或者直接发我一道 LeetCode 题目链接也行～"
            )
        else:
            # 用户未提交就放弃，或没有 verdict
            _guide_content = (
                "好的，这道题先放一放。接下来想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？\n\n"
                "也可以直接把一道 LeetCode 题目链接发给我哦～"
            )
        guide_msg = Message(role="tutor", content=_guide_content)

        # tutor_messages 不清空：保留完整对话历史供前端展示，
        # 追加下一题引导消息。LLM 上下文由 agent_dialog_history（当前题）+ context_summary（跨题摘要）承载。
        _existing_tutor = vals.get("tutor_messages") or []
        _display_history = list(_existing_tutor) + [guide_msg]

        graph.update_state(config, {
            "mode": "agent",
            "status": "dialog",
            "problem": None,
            "agent_dialog_complete": False,
            "agent_dialog_history": [guide_msg],
            "tutor_messages": _display_history,
            "tutor_messages_cutoff": len(_display_history),
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

    # ── Normal modes: critic flush → planner → generator ──
    # 2026-08-04 修复：旧实现用 update_state(as_node="critic_node") + invoke(None)，
    # 但 graph 此刻停在 wait_for_submit 的 interrupt 上——暂停期 update_state 会丢失
    # 挂起的中断，且 critic_node 根本不会运行，导致永远不出新题（原样返回旧题）。
    # 新实现：以 abandon 载荷 resume 该 interrupt，wait_for_submit_node 携带
    # pending_abandon/next_preference，wait_for_submit_router 路由到 critic_node，
    # 走 critic(ABANDON) → planner → generator → wait 完成换题。

    # Set up progress messages (frontend polls /state during generation)
    _generation_progress[sid] = ["正在准备下一题…"]

    if "wait_for_submit_node" not in (state.next or ()):
        logger.warning("next-problem: session %s not paused at wait_for_submit (next=%s)",
                       sid, state.next)
        raise HTTPException(409, "当前会话不在等待提交状态，无法换题")

    await asyncio.to_thread(
        graph.invoke,
        Command(resume={"abandon": True, "preference": body.preference}),
        config,
    )

    # Read new state
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
