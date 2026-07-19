"""Run router — POST /session/{sid}/run."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.schemas.api import RunCodeRequest, RunCodeResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{sid}/run", response_model=RunCodeResponse)
async def run_code(sid: str, body: RunCodeRequest):
    """Run the user's code against visible test cases."""
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded in this session")

    from code_tutor_agent.db.database import get_problem_by_id
    problem_id = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(500, "Problem not found in database")

    visible = full.get("visible_test_cases", [])
    if not visible:
        test_cases = full.get("test_cases", [])
        visible = [tc for tc in test_cases if not tc.get("is_hidden", False)]

    from code_tutor_agent.sandbox.runner import run_solution
    _func_sig = full.get("function_signature", "") or ""
    results = run_solution(body.code, visible, function_signature=_func_sig)

    run_results = []
    all_pass = True
    for r in results:
        passed = r.status == "Passed"
        if not passed:
            all_pass = False
        # runner 的 test_case_id 是 0 基，直接作为 visible 列表下标。
        # （之前误用 test_case_id - 1，导致 期望/输入 与用例错位并环绕到最后一条）
        _vi = r.test_case_id
        run_results.append({
            "test_case_id": r.test_case_id,
            "passed": passed,
            "status": r.status,
            "detail": r.detail[:200] if r.detail else "",
            "input_args": visible[_vi].get("input_args", []) if _vi < len(visible) else [],
            "expected": visible[_vi].get("expected_output", "") if _vi < len(visible) else "",
            "runtime_ms": r.runtime_ms,
            "memory_kb": r.memory_kb,
        })

    try:
        graph.update_state(config, {"last_run_results": run_results})
    except Exception as exc:
        logger.warning("Failed to persist run results: %s", exc)

    return RunCodeResponse(
        session_id=sid, all_passed=all_pass,
        results=run_results, total=len(run_results),
        passed=sum(1 for r in run_results if r["passed"]),
    )