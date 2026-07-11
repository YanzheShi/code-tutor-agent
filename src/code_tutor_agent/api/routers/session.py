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
from code_tutor_agent.schemas.api import CreateSessionRequest, SubmitRequest, SubmitResponse, SessionStateResponse
from code_tutor_agent.schemas.state import SessionState, ProblemMeta

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