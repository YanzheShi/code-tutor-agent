"""FastAPI application — CodeTutor Agent HTTP endpoints.

Three endpoints (D1 MVP):

- ``POST /session``       — create session, return immediately (background generation)
- ``POST /session/{sid}/submit`` — submit code, run judge+tutor, return hint
- ``GET  /session/{sid}/state``  — poll current state snapshot (includes progress)
- ``POST /session/{sid}/run``   — run code against visible test cases (new)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from code_tutor_agent.graph.graph import compile_graph
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.api import (
    AdminLoginRequest,
    AdminPasswordRequest,
    AdminProblemOut,
    AdminUpdateProblemRequest,
    CreateSessionRequest,
    LeetCodeParseRequest,
    LeetCodeParseResponse,
    RunCodeRequest,
    RunCodeResponse,
    SessionStateResponse,
    SubmitRequest,
    SubmitResponse,
)
from code_tutor_agent.schemas.state import ProblemMeta, SessionState

load_dotenv()

# ── Logging: ensure all module-level loggers (graph, nodes, api) output to stderr ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    force=True,  # override any previous config (e.g. uvicorn's)
)
logger = logging.getLogger(__name__)

# ── Global (lazy-initialised) ──
_graph: CompiledStateGraph | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: compile the LangGraph once and hold the reference."""
    global _graph
    logger.info("Compiling LangGraph …")
    _graph = compile_graph()
    logger.info("LangGraph ready ✓")
    yield


app = FastAPI(
    title="CodeTutor Agent",
    version="0.1.0",
    description="AI-powered coding tutor with multi-agent architecture",
    lifespan=lifespan,
)

# ── CORS: Vite dev server (5173) → backend (8765) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────
#  Helper: serialise state for the wire
# ──────────────────────────────────────────────


def _serialise_state(state: Any) -> dict:
    """Convert a session state to a JSON-safe dict with safe defaults."""
    if isinstance(state, dict):
        d = state
    else:
        d = state.model_dump() if hasattr(state, "model_dump") else {}

    sid = d.get("session_id", "")

    # Read progress from shared graph store
    progress = _generation_progress.get(sid, [])

    # Build safe problem dict (fields may be missing from old checkpoints)
    raw_problem = d.get("problem")
    problem = None
    if raw_problem:
        pd = raw_problem.model_dump() if hasattr(raw_problem, "model_dump") else raw_problem
        # Inject defaults for new fields that old checkpoints lack
        pd.setdefault("starter_code", "")
        pd.setdefault("visible_test_cases", [])
        problem = pd

    return {
        "session_id": sid,
        "topic": d.get("topic", ""),
        "difficulty": d.get("difficulty", ""),
        "mode": d.get("mode", ""),
        "status": d.get("status", "generating"),
        "problem": problem,
        "submissions": [
            {
                "index": s.get("index") if isinstance(s, dict) else s.index,
                "code": (s.get("code", "")[:200] if isinstance(s, dict) else s.code[:200]),
                "language": s.get("language", "python") if isinstance(s, dict) else s.language,
                "judge_results": [r.model_dump() if hasattr(r, "model_dump") else r for r in (s.get("judge_results", []) if isinstance(s, dict) else s.judge_results)],
                "hint_level_given": s.get("hint_level_given", 0) if isinstance(s, dict) else s.hint_level_given,
            }
            for s in (d.get("submissions") or [])
        ],
        "tutor_messages": [
            m.model_dump() if hasattr(m, "model_dump") else m
            for m in (d.get("tutor_messages") or [])
        ],
        "hint_level": d.get("hint_level", 0),
        "last_verdict": d.get("last_verdict"),
        "last_review_payload": d.get("last_review_payload"),
        "error_message": d.get("error_message", ""),
        "progress_messages": progress,
    }


# ──────────────────────────────────────────────
#  Background generation
# ──────────────────────────────────────────────


async def _run_generation(sid: str, initial_dict: dict):
    """Run the full graph in a background task, writing progress along the way.

    Day2 flow (see nodes/generator.py docstring for full flow diagram):

        Graph invoke → problem+sample TCs ready → user sees problem
        ↓
        Background: generate full test suite (random + LLM boundary)
        ↓
        Update DB with complete test_cases
    """
    global _graph
    if _graph is None:
        _generation_progress.setdefault(sid, []).append("❌ 系统未就绪")
        return

    config = {"configurable": {"thread_id": sid}}

    try:
        initial = SessionState(**initial_dict)
        _generation_progress.setdefault(sid, []).append("🚀 开始生成题目…")

        await asyncio.to_thread(_graph.invoke, initial.model_dump(), config)
        _generation_progress.setdefault(sid, []).append("✅ 题目已就绪，正在后台生成完整测试用例…")

        # ── Step 2: Background test generation (while user writes code) ──
        # Fetch the state to get problem_id
        try:
            state = _graph.get_state(config)
            problem = state.values.get("problem")
            if problem:
                pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
                if pid:
                    # Only run background test gen if we have a brute solution
                    brute_code = state.values.get("_brute_code", "") or ""
                    if brute_code:
                        await _generate_complex_tests(pid, sid)
                    else:
                        _generation_progress.setdefault(sid, []).append("📝 LeetCode 题目已导入（跳过后台测试生成）")

        except Exception as exc:
            logger.warning("Background test generation failed: %s", exc)
            _generation_progress.setdefault(sid, []).append("⚠️ 部分测试用例生成失败")

    except Exception as exc:
        logger.exception("Background generation failed for %s", sid)
        _generation_progress.setdefault(sid, []).append(f"❌ 生成失败: {exc}")


async def _generate_complex_tests(problem_id: int, sid: str):
    """Generate full test suite in background after user gets the problem.

    Day2 flow step:
        1. Fetch problem from DB → get brute_code + function_signature
        2. Generate 10-15 random inputs via local Python
        3. Run brute_solution on all → get expected_outputs
        4. **(NEW)** Call LLM (Prompt B) to generate boundary test cases
        5. Run brute_solution on boundary cases to validate expected_outputs
        6. Merge sample + random + boundary cases
        7. Save to DB via update_problem_test_cases()

    This runs in the background while the user is writing code,
    so by the time they click "Submit", the full test suite is ready.
    """
    from code_tutor_agent.db.database import get_problem_by_id, update_problem_test_cases
    from code_tutor_agent.sandbox.input_generator import generate_random_inputs
    from code_tutor_agent.sandbox.runner import run_solution

    logger.info("▶ _generate_complex_tests() — problem_id=%d", problem_id)

    full = get_problem_by_id(problem_id)
    if not full:
        logger.warning("Problem %d not found for background test gen", problem_id)
        return

    brute_code = full.get("brute_solution", "")
    func_sig = full.get("function_signature", "")

    if not brute_code:
        logger.warning("No brute_code for problem %d — skipping bg test gen", problem_id)
        return

    _generation_progress.setdefault(sid, []).append("🧪 正在生成更多测试用例…")

    # ── Step 1: Generate 10 random inputs ──
    random_inputs = generate_random_inputs(func_sig, count=12, seed=problem_id)
    logger.info("Generated %d random inputs", len(random_inputs))

    if not random_inputs:
        logger.warning("No random inputs generated")
        return

    # ── Step 2: Run brute force on all random inputs ──
    _generation_progress.setdefault(sid, []).append(f"🔧 正在运行暴力解验证 {len(random_inputs)} 个用例…")
    all_tcs: list[dict] = []
    for idx, inp in enumerate(random_inputs):
        tc = {
            "input_args": inp,
            "expected_output": "",
            "is_hidden": idx >= 4,  # first 4 visible, rest hidden
            "explanation": f"随机生成测试 {idx+1}",
        }
        results = run_solution(brute_code, [tc], timeout=10.0)
        if results:
            r = results[0]
            actual = r.detail or ""
            if actual:
                tc["expected_output"] = actual
                all_tcs.append(tc)
            else:
                logger.warning("TC %d: no actual output (%s)", idx, r.status)

    # ── Step 3: LLM boundary test cases (Prompt B) ──
    _generation_progress.setdefault(sid, []).append("🤖 正在生成边界测试用例…")
    try:
        from code_tutor_agent.config import get_llm
        from code_tutor_agent.prompts.generate_boundary_cases import (
            GENERATE_BOUNDARY_SYSTEM,
            GENERATE_BOUNDARY_USER,
        )

        existing_cases_str = "\n".join(
            f"  #{i+1}: input_args={tc.get('input_args', [])} → {tc.get('expected_output', '')}"
            for i, tc in enumerate(all_tcs[:4])
        )

        constraints_str = "\n".join(f"  - {c}" for c in (full.get("constraints") or []))

        prompt_user = GENERATE_BOUNDARY_USER.format(
            title=full.get("title", ""),
            description=full.get("description", ""),
            difficulty=full.get("difficulty", ""),
            function_signature=func_sig,
            constraints=constraints_str,
            brute_code=brute_code,
            existing_cases=existing_cases_str,
            count=8,
        )

        llm = get_llm("agnes", temperature=0.5)
        resp = llm.invoke([
            ("system", GENERATE_BOUNDARY_SYSTEM),
            ("human", prompt_user),
        ])
        content = resp.content if hasattr(resp, "content") else str(resp)

        # Parse the JSON array from response (strip any markdown fences)
        import re
        json_match = re.search(r"\[.*?\]", content, re.DOTALL)
        if json_match:
            import json
            boundary_cases = json.loads(json_match.group(0))
            logger.info("LLM generated %d boundary test cases", len(boundary_cases))

            # Validate each boundary case by running brute force
            _generation_progress.setdefault(sid, []).append(f"🔧 正在验证 {len(boundary_cases)} 个边界用例…")
            for bc in boundary_cases:
                # Run brute solution on this case to get the actual expected output
                results = run_solution(brute_code, [{
                    "input_args": bc.get("input_args", []),
                    "expected_output": bc.get("expected_output", ""),
                }], timeout=10.0)
                if results and results[0].detail:
                    bc["expected_output"] = results[0].detail  # overwrite with actual
                    bc["is_hidden"] = True
                    bc["explanation"] = bc.get("explanation", "LLM 生成的边界用例")
                    all_tcs.append(bc)
                else:
                    logger.warning("Boundary case validation failed, skipping: %s", bc.get("explanation", ""))
    except Exception as exc:
        logger.warning("Prompt B (boundary LLM) failed: %s — continuing with random-only suite", exc)

    # ── Step 4: Merge with sample test cases ──
    existing_tcs = full.get("test_cases", [])
    sample_tcs = [tc for tc in existing_tcs if not tc.get("is_hidden", False)][:2]

    full_suite = sample_tcs + all_tcs
    logger.info("Full test suite: %d sample + %d generated (incl. %d boundary) = %d total",
                len(sample_tcs), len(all_tcs),
                len(all_tcs) - len(random_inputs[:4]),  # rough count of boundary cases
                len(full_suite))

    # ── Step 5: Save to DB ──
    update_problem_test_cases(problem_id, full_suite)
    _generation_progress.setdefault(sid, []).append(f"✅ 共 {len(full_suite)} 个测试用例已就绪（含 LLM 边界用例）")
    logger.info("Completed background test generation for problem %d", problem_id)


# ──────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────


@app.post("/session")
async def create_session(body: CreateSessionRequest | None = None):
    """Create a new tutoring session (background generation).

    Returns immediately with the session ID.  Poll ``GET /session/{sid}/state``
    to track progress and get the final problem.
    """
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")

    sid = str(uuid.uuid4())
    config = {"configurable": {"thread_id": sid}}

    # Initial state with user preferences
    initial_dict = {"session_id": sid}
    if body:
        if body.topic:
            initial_dict["topic"] = body.topic
        if body.difficulty:
            initial_dict["difficulty"] = body.difficulty
        if body.mode:
            initial_dict["mode"] = body.mode
        # Pass through parsed LeetCode data if present
        if body.leetcode:
            initial_dict["leetcode"] = body.leetcode

    # ── LeetCode fast-path: use parsed examples as test cases, skip graph ──
    if body and body.leetcode:
        le_data = body.leetcode
        parsed_tcs = le_data.get("parsed_test_cases") or []
        if parsed_tcs:
            logger.info("LeetCode fast-path — %d parsed test cases from examples", len(parsed_tcs))
            _generation_progress[sid] = ["📥 正在导入 LeetCode 题目…"]

            from code_tutor_agent.db.database import save_problem
            from code_tutor_agent.schemas.state import ProblemMeta, Message as TutorMsg

            visible_tcs = [
                {
                    "input_args": tc.get("input_args", []),
                    "expected_output": tc.get("expected_output", ""),
                    "explanation": tc.get("explanation", ""),
                }
                for tc in parsed_tcs
            ]

            problem_dict = {
                "title": le_data.get("title", ""),
                "topic": le_data.get("topic", body.topic or ""),
                "difficulty": le_data.get("difficulty", body.difficulty or "medium"),
                "description": le_data.get("description", ""),
                "description_html": le_data.get("description_html", ""),
                "starter_code": le_data.get("starter_code", ""),
                "brute_solution": "",
                "function_signature": "",
                "test_cases": parsed_tcs,
            }
            problem_id = save_problem(problem_dict)

            meta = ProblemMeta(
                problem_id=problem_id,
                title=problem_dict["title"],
                topic=problem_dict.get("topic", ""),
                difficulty=problem_dict.get("difficulty", "medium"),
                description=problem_dict.get("description", ""),
                starter_code=problem_dict.get("starter_code", ""),
                visible_test_cases=visible_tcs,
                description_html=le_data.get("description_html", ""),
            )

            initial = SessionState(
                session_id=sid,
                problem=meta,
                status="awaiting_submit",
                topic=meta.topic,
                difficulty=meta.difficulty,
                tutor_messages=[TutorMsg(role="tutor", content=f"从 LeetCode 导入 **{meta.title}**！编辑器里已填入模板代码。")],
            )
            _graph.invoke(initial.model_dump(), config)

            state = _graph.get_state(config)
            return _serialise_state(state.values)

        # ── Normal path: background generation via graph ──
        _generation_progress[sid] = []

    # Kick off background generation
    asyncio.create_task(_run_generation(sid, initial_dict))

    return {"session_id": sid, "status": "generating"}


@app.post("/session/{sid}/submit", response_model=SubmitResponse)
async def submit_code(sid: str, body: SubmitRequest):
    """Resume a paused session with user-submitted code."""
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")

    config = {"configurable": {"thread_id": sid}}

    try:
        state = _graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    if state.values.get("status") == "done":
        raise HTTPException(400, "Session is already done — create a new session")

    logger.info(
        "POST /session/%s/submit → code=%d chars, lang=%s",
        sid, len(body.code), body.language,
    )

    result = _graph.invoke(
        Command(resume={"code": body.code, "language": body.language}),
        config,
    )

    state = _graph.get_state(config)
    values = state.values

    # ── Persist submission to SQLite ──
    try:
        problem = values.get("problem")
        subs = values.get("submissions", [])
        if subs and problem:
            pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
            last_sub = subs[-1] if isinstance(subs, list) else subs
            from code_tutor_agent.db.database import save_submission
            verdict = last_sub.get("last_verdict") or last_sub.get("verdict") or values.get("last_verdict", "")
            if verdict:
                save_submission(pid, body.code, verdict, last_sub.get("judge_results", []))
    except Exception as exc:
        logger.warning("Failed to persist submission: %s", exc)

    tutor_messages = values.get("tutor_messages", [])
    if tutor_messages:
        last = tutor_messages[-1]
        tutor_msg = last["content"] if isinstance(last, dict) else last.content
    else:
        tutor_msg = None

    return SubmitResponse(
        session_id=sid,
        status=values.get("status", "unknown"),
        verdict=values.get("last_verdict"),
        tutor_message=tutor_msg,
        hint_level=values.get("hint_level", 0),
    )


@app.post("/session/{sid}/run", response_model=RunCodeResponse)
async def run_code(sid: str, body: RunCodeRequest):
    """Run the user's code against visible test cases (no graph invocation).

    Returns pass/fail for each visible test case without running judge/tutor.
    """
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")

    config = {"configurable": {"thread_id": sid}}

    try:
        state = _graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded in this session")

    # Get problem data from DB
    from code_tutor_agent.db.database import get_problem_by_id
    problem_id = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(500, "Problem not found in database")

    # Run only visible test cases (from visible_test_cases_json column)
    visible = full.get("visible_test_cases", [])
    if not visible:
        # Fallback: filter from full test cases by is_hidden
        test_cases = full.get("test_cases", [])
        visible = [tc for tc in test_cases if not tc.get("is_hidden", False)]

    from code_tutor_agent.sandbox.runner import run_solution
    results = run_solution(body.code, visible)

    run_results = []
    all_pass = True
    for r in results:
        passed = r.status == "Passed"
        if not passed:
            all_pass = False
        run_results.append({
                    "test_case_id": r.test_case_id,
                    "passed": passed,
                    "status": r.status,
                    "detail": r.detail[:200] if r.detail else "",
                    "input_args": visible[r.test_case_id - 1].get("input_args", []) if (r.test_case_id - 1) < len(visible) else [],
                    "expected": visible[r.test_case_id - 1].get("expected_output", "") if (r.test_case_id - 1) < len(visible) else "",
                    "runtime_ms": r.runtime_ms,
                                        "memory_kb": r.memory_kb,
                })

    # Persist run results to session state (survives tab switch / page reload)
    try:
        _graph.update_state(config, {"last_run_results": run_results})
    except Exception as exc:
        logger.warning("Failed to persist run results: %s", exc)

    return RunCodeResponse(
        session_id=sid,
        all_passed=all_pass,
        results=run_results,
        total=len(run_results),
        passed=sum(1 for r in run_results if r["passed"]),
    )


@app.get("/session/{sid}/state", response_model=SessionStateResponse)
async def get_session_state(sid: str):
    """Poll the current session state (includes progress during generation)."""
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")

    config = {"configurable": {"thread_id": sid}}

    try:
        state = _graph.get_state(config)
    except Exception:
        # Session not yet in checkpointer — still generating
        progress = _generation_progress.get(sid, [])
        return {
            "session_id": sid,
            "topic": "",
            "difficulty": "",
            "mode": "practice",
            "status": "generating",
            "problem": None,
            "submissions": [],
            "tutor_messages": [],
            "hint_level": 0,
            "last_verdict": None,
            "last_review_payload": None,
            "error_message": "",
            "progress_messages": progress,
        }

    return _serialise_state(state.values)


# ──────────────────────────────────────────────
#  Problem listing & existing-problem sessions
# ──────────────────────────────────────────────


@app.get("/problems")
async def list_problems():
    """List all problems in the database (for the \"pick from existing\" UI)."""
    from code_tutor_agent.db.database import get_all_problem_ids, get_problem_by_id

    ids = get_all_problem_ids()
    result = []
    for pid in ids[-50:]:  # last 50 only
        p = get_problem_by_id(pid)
        if p:
            result.append({
                "id": p["id"],
                "title": p.get("title", ""),
                "topic": p.get("topic", ""),
                "difficulty": p.get("difficulty", ""),
            })
    return {"problems": result}


@app.get("/problem/{problem_id}/submissions")
async def get_problem_submissions(problem_id: int):
    """Get persistent submission history for a problem."""
    from code_tutor_agent.db.database import get_submissions_by_problem
    return {"submissions": get_submissions_by_problem(problem_id)}


@app.get("/session/{sid}/reference")
async def get_reference_code(sid: str):
    """Get the reference solution for the problem in a session.

    Only visible after the user has achieved AC.
    """
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")
    config = {"configurable": {"thread_id": sid}}
    try:
        state = _graph.get_state(config)
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
    brute = full.get("brute_solution", "")
    return {"code": brute, "title": full.get("title", "")}


@app.post("/session/{sid}/chat/stream")
async def chat_with_tutor_stream(sid: str, body: dict):
    """Streaming chat with the AI tutor via Server-Sent Events.

    Each token is yielded as an SSE ``data:`` event.  The frontend
    accumulates tokens and renders them as they arrive.
    """
    from starlette.responses import StreamingResponse
    from code_tutor_agent.config import get_llm

    if _graph is None:
        raise HTTPException(503, "Graph not initialised")
    config = {"configurable": {"thread_id": sid}}
    try:
        state = _graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    message = (body or {}).get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message is empty")

    values = state.values
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    llm = get_llm("agnes", temperature=0.7, streaming=True)
    prompt = f"你是一个编程导师。用户正在做一道算法题「{title}」。\n用户当前的消息：{message}\n\n请给出有帮助的指导和建议，不要直接给出完整代码。回复控制在 200 字以内。"

    async def event_stream():
        full_reply = []
        try:
            async for chunk in llm.astream(prompt):
                token = chunk.content if hasattr(chunk, "content") else str(chunk)
                if token:
                    full_reply.append(token)
                    yield f"data: {token}\n\n"
        except Exception as exc:
            logger.warning("Streaming chat LLM failed: %s", exc)
            yield f"data: 【抱歉，我现在无法回答。请稍后再试。】\n\n"

        # Save final messages to state
        reply_text = "".join(full_reply)
        current_msgs = list(values.get("tutor_messages", []))
        current_msgs.append({"role": "user", "content": message})
        current_msgs.append({"role": "tutor", "content": reply_text})
        try:
            _graph.update_state(config, {"tutor_messages": current_msgs})
        except Exception as exc:
            logger.warning("Failed to save chat to state: %s", exc)
        yield "data: __DONE__\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/session/{sid}/chat")
async def chat_with_tutor(sid: str, body: dict):
    """Chat with the AI tutor about the current problem.

    Takes a user message, generates a contextual response via LLM,
    appends both to the session's tutor_messages, and returns the response.
    """
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")
    config = {"configurable": {"thread_id": sid}}
    try:
        state = _graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    message = (body or {}).get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message is empty")

    values = state.values
    problem = values.get("problem")
    title = problem.title if hasattr(problem, "title") else (problem.get("title", "") if problem else "")

    # Build a simple LLM response
    from code_tutor_agent.config import get_llm

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

    # Update state with both messages
    current_msgs = list(values.get("tutor_messages", []))
    current_msgs.append({"role": "user", "content": message})
    current_msgs.append({"role": "tutor", "content": reply})
    _graph.update_state(config, {"tutor_messages": current_msgs})

    return {"response": reply}


@app.post("/session/by-problem/{problem_id}")
async def create_session_with_existing(problem_id: int):
    """Create a session using an existing problem from the database."""
    if _graph is None:
        raise HTTPException(503, "Graph not initialised")

    from code_tutor_agent.db.database import get_problem_by_id

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    sid = str(uuid.uuid4())
    config = {"configurable": {"thread_id": sid}}

    # Build ProblemMeta directly — use visible_test_cases from DB column
    visible_tcs = full.get("visible_test_cases", [])
    if not visible_tcs:
        # Fallback: filter from full test cases by is_hidden
        raw_test_cases = full.get("test_cases", [])
        visible_tcs = [
            {"input_args": tc.get("input_args", []),
             "expected_output": tc.get("expected_output", ""),
             "explanation": tc.get("explanation", ""),}
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
    )

    # Persist state via graph invoke — we use an initial state that
    # skips planner + generator and goes straight to awaiting_submit
    initial_dict = {
        "session_id": sid,
        "problem": meta,
        "status": "awaiting_submit",
        "topic": meta.topic,
        "difficulty": meta.difficulty,
        "submissions": [],
        "hint_level": 0,
        "tutor_messages": [],
        "last_verdict": None,
        "error_message": "",
    }

    initial = SessionState(**initial_dict)
    _graph.invoke(initial.model_dump(), config)

    state = _graph.get_state(config)
    return _serialise_state(state.values)


# ──────────────────────────────────────────────
#  LeetCode URL parser
# ──────────────────────────────────────────────


@app.post("/leetcode/parse")
async def parse_leetcode(body: LeetCodeParseRequest):
    """Parse a LeetCode problem URL via GraphQL and return structured data.

    Uses the same ``code_tutor_agent.leetcode_fetcher`` module as the CLI script----------.
    """
    from code_tutor_agent.leetcode.leetcode_fetcher import fetch_problem, problem_to_api_dict

    url = body.url.strip().rstrip("/")
    logger.info("POST /leetcode/parse url=%s", url)

    match = re.search(r"/problems/([^/]+)", url)
    if not match:
        logger.warning("Invalid LeetCode URL (no /problems/ segment): %s", url)
        raise HTTPException(400, "无效的 LeetCode 链接，请粘贴完整题目 URL")

    slug = match.group(1)
    domain = "leetcode.cn" if ".cn" in url else "leetcode.com"
    logger.info("Fetching slug=%s domain=%s", slug, domain)

    try:
        p = fetch_problem(slug, domain=domain)
    except ValueError as exc:
        logger.error("LeetCode fetch error: %s", exc)
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Unexpected error fetching LeetCode problem")
        raise HTTPException(502, f"获取题目失败: {exc}")

    if not p.title:
        logger.warning("Problem '%s' returned empty title", slug)
        raise HTTPException(404, f"Problem '{slug}' 未在 LeetCode 找到")

    data = problem_to_api_dict(p)
    logger.info("LeetCode parse OK: %s (%d examples, %d tags)", data["title"], len(data["examples"]), len(data["tags"]))
    return LeetCodeParseResponse(**data)


@app.get("/health")
async def health():
    """Simple health check."""
    return {"status": "ok", "graph_ready": _graph is not None}


# ──────────────────────────────────────────────
#  Admin endpoints (password-protected)
# ──────────────────────────────────────────────

_ADMIN_PASSWORD: str | None = None


def _get_admin_password() -> str | None:
    """Lazy-load admin password from env."""
    global _ADMIN_PASSWORD
    if _ADMIN_PASSWORD is None:
        _ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
    return _ADMIN_PASSWORD


def _verify_admin(request_body: dict) -> bool:
    """Check if the password matches ADMIN_PASSWORD."""
    expected = _get_admin_password()
    if not expected:
        return True  # No password configured → allow all
    provided = (request_body or {}).get("password", "")
    return provided == expected


@app.post("/admin/login")
async def admin_login(body: AdminLoginRequest):
    """Verify admin password. Returns success if password matches."""
    expected = _get_admin_password()
    if not expected:
        return {"ok": True, "message": "Admin mode (no password configured)"}
    if body.password == expected:
        return {"ok": True, "message": "登录成功"}
    raise HTTPException(401, "密码错误")


@app.post("/admin/problems")
async def admin_list_problems(body: AdminPasswordRequest = AdminPasswordRequest()):
    """List all problems with full details. Requires admin password in body."""
    if not _verify_admin(body.model_dump()):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_all_problem_ids, get_problem_by_id

    ids = get_all_problem_ids()
    result = []
    for pid in ids:
        p = get_problem_by_id(pid)
        if p:
            result.append(AdminProblemOut(
                id=p["id"],
                title=p.get("title", ""),
                topic=p.get("topic", ""),
                difficulty=p.get("difficulty", ""),
                description=p.get("description", ""),
                visible_test_cases_list=p.get("visible_test_cases", []),
                test_cases_list=p.get("test_cases", []),
                brute_solution=p.get("brute_solution", ""),
                starter_code=p.get("starter_code", ""),
                novelty_score=p.get("novelty_score", 7.0),
                created_at=p.get("created_at", ""),
            ))
    return {"problems": [p.model_dump(by_alias=True) for p in result]}


@app.put("/admin/problem/{problem_id}")
async def admin_update_problem(problem_id: int, body: AdminUpdateProblemRequest):
    """Update a problem. Only provided fields are updated. Requires admin password.

    Testcase handling:
    - test_cases → updates test_cases_json (判题用全量套件)
    - visible_test_cases → updates visible_test_cases_json (前台运行用可见套件)
    If only test_cases is provided and visible_test_cases is not, the visible
    subset is derived from non-hidden cases in test_cases.
    """
    admin_body = body if isinstance(body, dict) else body.model_dump(exclude_none=True)
    if not _verify_admin(admin_body):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_problem_by_id

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    updates = []
    params = []
    if body.title is not None:
        updates.append("title = ?"); params.append(body.title)
    if body.description is not None:
        updates.append("description = ?"); params.append(body.description)
    if body.topic is not None:
        updates.append("topic = ?"); params.append(body.topic)
    if body.difficulty is not None:
        updates.append("difficulty = ?"); params.append(body.difficulty)
    if body.test_cases is not None:
        import json
        updates.append("test_cases_json = ?"); params.append(json.dumps(body.test_cases, ensure_ascii=False))
    if body.visible_test_cases is not None:
        import json
        updates.append("visible_test_cases_json = ?"); params.append(json.dumps(body.visible_test_cases, ensure_ascii=False))
    if body.brute_solution is not None:
        updates.append("brute_solution = ?"); params.append(body.brute_solution)
    if body.starter_code is not None:
        updates.append("starter_code = ?"); params.append(body.starter_code)
    if body.novelty_score is not None:
        updates.append("novelty_score = ?"); params.append(body.novelty_score)

    # Auto-derive visible_test_cases from test_cases if test_cases changed but visible didn't
    if body.test_cases is not None and body.visible_test_cases is None:
        import json
        derived_visible = [tc for tc in body.test_cases if not tc.get("is_hidden", False)]
        if derived_visible:
            updates.append("visible_test_cases_json = ?"); params.append(json.dumps(derived_visible, ensure_ascii=False))

    if updates:
        params.append(problem_id)
        from code_tutor_agent.db.database import _get_conn
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE problems SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        conn.close()
    return {"ok": True, "message": f"Problem {problem_id} updated"}


@app.post("/admin/problem/{problem_id}/delete")
async def admin_delete_problem(problem_id: int, body: AdminPasswordRequest = AdminPasswordRequest()):
    """Delete a problem. Requires admin password in body."""
    if not _verify_admin(body.model_dump()):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_problem_by_id, _get_conn

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM submissions WHERE problem_id = ?", (problem_id,))
    cursor.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
    conn.commit()
    conn.close()

    return {"ok": True, "message": f"Problem {problem_id} deleted"}