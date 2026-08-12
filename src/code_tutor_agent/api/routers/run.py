"""Run router — POST /session/{sid}/run."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from code_tutor_agent.api.deps import get_graph, pause_safe_update
from code_tutor_agent.db.database import get_problem_by_id
from code_tutor_agent.leetcode.leetcode_fetcher import extract_signature_from_solution
from code_tutor_agent.observability import build_run_config
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.api import RunCodeRequest, RunCodeResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{sid}/run", response_model=RunCodeResponse)
async def run_code(sid: str, body: RunCodeRequest):
    """Run the user's code against visible test cases."""
    graph = get_graph()
    config = build_run_config(sid, run_name="run_code")

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded in this session")

    problem_id = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(500, "Problem not found in database")

    visible = full.get("visible_test_cases", [])
    if not visible:
        test_cases = full.get("test_cases", [])
        visible = [tc for tc in test_cases if not tc.get("is_hidden", False)]

    _func_sig = full.get("function_signature", "") or ""
    # ── 兜底：从 optimal_solution 提取签名覆盖 DB 值 ──
    #
    # 背景：LLM 出题时经常把 ListNode.__init__(val=0, next=None) 的参数
    # 当成 function_signature 输出，导致 DB 中存了 val=0,next=None -> None。
    # 这个错误签名会让 runner 不知道把数组 [1,2,3] 转成 ListNode 对象，
    # 用户代码访问 .val / .next 时报错。
    #
    # 解法：从 optimal_solution 的 Solution 方法提取签名（有 P0-1 自验证兜底），
    # 覆盖 DB 中可能错误的值。见 leetcode_fetcher.extract_signature_from_solution。
    _optimal_code = full.get("optimal_solution", "") or ""
    if _optimal_code:
        _extracted = extract_signature_from_solution(_optimal_code)
        if _extracted and _extracted != _func_sig:
            logger.info("Overriding function_signature from optimal_solution: %s", _extracted[:80])
            _func_sig = _extracted
    # 跑用户代码是同步阻塞（subprocess），必须丢进线程池，否则会冻结事件循环、
    # 与 SSE 推流/其他请求互相拖累；再套一层硬超时，防个别用例死循环拖垮整个请求。
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(run_solution, body.code, visible, function_signature=_func_sig),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "运行超时（可能代码含死循环或用例过大）")

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
        # 暂停安全写入：直接 update_state 会丢失 wait_for_submit 的挂起中断，
        # 导致后续 /submit 的 resume 空转（见 deps.pause_safe_update）。
        pause_safe_update(graph, config, {"last_run_results": run_results})
    except Exception as exc:
        logger.warning("Failed to persist run results: %s", exc)

    return RunCodeResponse(
        session_id=sid, all_passed=all_pass,
        results=run_results, total=len(run_results),
        passed=sum(1 for r in run_results if r["passed"]),
    )