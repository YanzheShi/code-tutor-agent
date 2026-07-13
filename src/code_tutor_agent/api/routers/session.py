"""Session router — create session, submit code, by-problem, state, reference."""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.api.serializers import serialize_state, empty_state
from code_tutor_agent.api.services.generation import run_generation, run_fast_path
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.api import CreateSessionRequest, NextProblemReq, NextProblemResp, SubmitRequest, SubmitResponse, SessionStateResponse
from code_tutor_agent.schemas.state import SessionState, ProblemMeta, Message

logger = logging.getLogger(__name__)
router = APIRouter()


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

    _generation_progress[sid] = []
    asyncio.create_task(run_generation(sid, initial_dict))
    return {"session_id": sid, "status": "generating"}


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
                last_row_id = save_submission(pid, body.code, verdict, serialised)
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
    """Poll the current session state."""
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

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

    vals = state.values
    mode = vals.get("mode", "practice")

    # ── Agent mode: re-enter the tutor dialog (keep history, no new problem) ──
    if mode == "agent":
        from code_tutor_agent.progress import _generation_progress
        _generation_progress[sid] = ["回到与导师的对话…"]
        # 清掉题目与出题参数，保留完整对话历史；
        # as_node 让 graph 干净停在 dialog（清掉 solving 态的 wait_for_submit 中断）
        #
        # 重要：update_state(as_node="agent_dialog_node") 后 graph.invoke(None)
        # 是空操作（agent_dialog_node → __end__，无后续节点），必须在本层直接
        # 构建"下一题"引导消息并写入 tutor_messages，不能依赖 agent_dialog_node 生成
        _full_history = vals.get("tutor_messages") or vals.get("agent_dialog_history") or []
        # 统一转为 Message 对象，避免混入 dict 导致 _build_transcript 中 msg.role 报错
        _norm_history: list[Message] = []
        for m in _full_history:
            if isinstance(m, Message):
                _norm_history.append(m)
            elif isinstance(m, dict):
                _norm_history.append(Message(role=m.get("role", "tutor"), content=m.get("content", "")))
            else:
                _norm_history.append(Message(role="tutor", content=str(m)))
        _updated_history = _norm_history + [Message(
            role="tutor",
            content=(
                "上一题已完成！接下来想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？"
            ),
        )]
        graph.update_state(config, {
            "status": "dialog",
            "problem": None,
            "agent_dialog_complete": False,
            "agent_dialog_history": _updated_history,
            "tutor_messages": _updated_history,
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