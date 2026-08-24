"""Agent 判题节点 — 通过 Judge0 进行 LLM 驱动的判题 LangGraph 节点。

该节点在 agent 模式下替代传统的机械判题节点 judge_node。
它不直接比较期望输出与实际输出，而是：
  1. 通过 Judge0 对测试用例执行用户代码
  2. 将原始结果传给 LLM 进行解读

运行/提交统一收口到本节点：
  - scope="sample"（运行按钮）→ 只跑 visible_test_cases，写诊断 last_run_results，不写画像
  - scope="full"（提交按钮）→ 跑全量用例（含边界），AC 写 v2 画像并 done

节点流转（由 graph.agent_judge_router 路由，本节点只返回 state 更新）：
  WA               → agent_tutor_node
  full + AC        → update_profile_node → critic_node
  sample + AC / 运行 → wait_for_submit_node（不写画像、不 done，保持循环）
"""

from __future__ import annotations

import logging
from typing import Any

from code_tutor_agent.agents.agent_judge import (
    JudgeAnalysis,
    analyze_judge_results,
    _deterministic_verdict,
)
from code_tutor_agent.db.database import get_problem_by_id
from code_tutor_agent.leetcode.leetcode_fetcher import extract_signature_from_solution
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import JudgeResult, SessionState

logger = logging.getLogger(__name__)

# Timeout per test case during agent judging
AGENT_JUDGE_TIMEOUT = 5.0


def _to_run_results(results: list, test_cases: list) -> list[dict]:
    """Convert RunnerResult list → RunCodeResponse-shaped dicts (mirrors old run.py)."""
    run_results: list[dict] = []
    for r in results:
        passed = r.status == "Passed"
        _vi = r.test_case_id
        tc = test_cases[_vi] if 0 <= _vi < len(test_cases) else {}
        run_results.append({
            "test_case_id": r.test_case_id,
            "passed": passed,
            "status": r.status,
            "detail": r.detail[:200] if r.detail else "",
            # RunnerResult 自身已带 input_args（执行时用到的真实输入），优先用它，
            # 避免依赖 test_cases[_vi] 索引错位或 tc 非 dict 时把 input 丢成空列表。
            "input_args": list(getattr(r, "input_args", None)
                               or (tc.get("input_args", []) if isinstance(tc, dict) else [])),
            "expected": tc.get("expected_output", "") if isinstance(tc, dict) else "",
            "explanation": tc.get("explanation", "") if isinstance(tc, dict) else "",
            "runtime_ms": r.runtime_ms,
            "memory_kb": r.memory_kb,
        })
    return run_results


def _resolve_inputs(state: SessionState) -> "dict | tuple":
    """校验输入并加载 problem + 测试用例。

    Returns:
        - dict: 错误 update（``{"status": "error", "error_message": ...}``），
          当任一前置条件不满足时直接返回，由路由 → END。
        - tuple: ``(code, is_run, problem_dict, test_cases)``，全部满足时。
    """
    if not state.submissions:
        logger.warning("No submission found — routing to error")
        return {"status": "error", "error_message": "No submission to judge"}

    last = state.submissions[-1]
    is_run = last.is_run
    code = last.code

    problem_id = state.problem.problem_id if state.problem else 0
    if not problem_id:
        logger.warning("No problem_id in state — routing to error")
        return {"status": "error", "error_message": "No problem loaded"}

    problem_dict = get_problem_by_id(problem_id)
    if not problem_dict:
        logger.warning("Problem %d not found in DB", problem_id)
        return {"status": "error", "error_message": f"Problem {problem_id} not found"}

    # ── 按 scope 选测试用例 ──
    if state.judge_scope == "sample":
        test_cases = problem_dict.visible_test_cases
        if not test_cases:
            # 兜底：从无 hidden 标记的用例里取可见集
            test_cases = [tc for tc in problem_dict.test_cases if not tc.get("is_hidden", False)]
    else:
        test_cases = problem_dict.test_cases
    if not test_cases:
        logger.warning("No test cases for problem %d", problem_id)
        return {"status": "error", "error_message": "No test cases available"}

    return code, is_run, problem_dict, test_cases


def _resolve_function_signature(problem_dict: Any) -> str:
    """解析 function_signature，并用 optimal_solution 提取的签名兜底覆盖。

    同 judge.py 和 run.py 的做法：当 DB 存的签名缺失/不准时，从参考解重新提取。
    """
    func_sig = getattr(problem_dict, "function_signature", "") or ""
    optimal_code = getattr(problem_dict, "optimal_solution", "") or ""
    if optimal_code:
        extracted = extract_signature_from_solution(optimal_code)
        if extracted and extracted != func_sig:
            logger.info("Overriding function_signature from optimal_solution: %s", extracted[:80])
            func_sig = extracted
    return func_sig


def _build_base_result(raw_results: list, test_cases: list | None = None) -> JudgeResult:
    """由执行结果构造 base JudgeResult：首个失败用例（结构化）或 AC 汇总文案。"""
    _status_map = {
        "Passed": "AC",
        "Wrong Answer": "WA",
        "Runtime Error": "RE",
        "TLE": "TLE",
        "Time Limit Exceeded": "TLE",
    }
    first_fail = next((r for r in raw_results if r.status != "Passed"), None)
    if first_fail is not None:
        _tc = test_cases[first_fail.test_case_id] if test_cases and 0 <= first_fail.test_case_id < len(test_cases) else {}
        return JudgeResult(
            status=_status_map.get(first_fail.status, "WA"),
            phase="base",
            detail=first_fail.detail or "",
            runtime_ms=first_fail.runtime_ms,
            memory_kb=first_fail.memory_kb,
            input_args=list(first_fail.input_args or []),
            expected_output=first_fail.expected_output or "",
            actual_output=first_fail.actual_output or "",
            explanation=_tc.get("explanation", "") if isinstance(_tc, dict) else "",
        )
    return JudgeResult(
        status="AC",
        phase="base",
        detail=f"{sum(1 for r in raw_results if r.status == 'Passed')}/{len(raw_results)} passed",
        runtime_ms=sum(r.runtime_ms for r in raw_results),
    )


def _run_analysis(
    state: SessionState,
    problem_dict: Any,
    code: str,
    raw_results: list,
    deterministic_verdict: str,
) -> JudgeAnalysis:
    """分发判题分析：sample=本地快速反馈（跳过 LLM）；full=LLM 驱动分析。

    两种路径 verdict 都永远以执行引擎客观结果为准，不信任 LLM 主观判断。

    - 提交(full)：走 LLM 生成 warm_feedback + repair_suggestion（面试官式诊断）。
    - 运行(sample)：跳过 LLM，直接用执行引擎客观结果拼简短诊断反馈——
      运行=快速自测，不该等 LLM（重构前 run.py 纯执行约 1~2s；统一收口后误入 LLM 致 14~85s）。
    """
    if state.judge_scope == "sample":
        passed = sum(1 for r in raw_results if r.status == "Passed")
        total = len(raw_results)
        _fb = (
            f"样例用例 {passed}/{total} 通过 ✅"
            if deterministic_verdict == "AC"
            else f"样例用例 {passed}/{total} 通过，还有问题，继续调试～"
        )
        logger.info("运行(sample) 跳过 LLM 判题分析，verdict=%s", deterministic_verdict)
        return JudgeAnalysis(
            verdict=deterministic_verdict,
            warm_feedback=_fb,
            repair_suggestion="",
        )
    return analyze_judge_results(
        code=code,
        title=problem_dict.title,
        difficulty=problem_dict.difficulty or state.difficulty,
        topic=problem_dict.topic or state.topic,
        description=problem_dict.description,
        results=raw_results,
        forced_verdict=deterministic_verdict,
    )


def _apply_side_effects(
    state: SessionState,
    analysis: JudgeAnalysis,
    raw_results: list,
    test_cases: list,
    is_run: bool,
    feedback_msg: str,
    update: dict,
    code: str = "",
) -> None:
    """应用画像写入、sample 诊断与路由 status，原地修改 ``update``。

    - 兼容旧版 v1 全局画像：仅真实提交（非运行）写，直接命中 DB（微决策 1）。
    - sample 诊断：写 last_run_results，样例全过时追加鼓励提交提示。
    - 路由：full+AC→done（交由 update_profile_node 落 v2 画像）；非 AC→tutoring；
      sample+AC→awaiting_submit（不写画像、不 done，保持运行循环）。
    """
    # ── 兼容旧版 v1 全局画像（综合熟练度/做题数等顶部指标）──
    if not is_run:
        try:
            from code_tutor_agent.db.database import update_profile_on_result
            topic = state.problem.topic if state.problem else "未知"
            update_profile_on_result(topic=topic, verdict=analysis.verdict)
        except Exception:
            logger.warning("Agent v1 profile update failed (non-fatal)", exc_info=True)

        # ── 错误模式画像（error-mode-tracking 特性，fire-and-forget）──
        # 仅真实提交触发；AC 也跑（捕捉"提交前自查修复"的弱项），非 AC 额外给
        # 判题失败补充 feeder ×1.3。分析在守护线程后台进行，不阻塞判题返回。
        try:
            from code_tutor_agent.profile.edit_trace_analyzer import (
                fire_and_forget_error_mode_analysis,
                judge_failure_to_tags,
            )
            prob = state.problem
            if isinstance(prob, dict):
                topic = prob.get("topic", "") or "未知"
                description = prob.get("description", "") or ""
            else:
                topic = getattr(prob, "topic", "") or "未知"
                description = getattr(prob, "description", "") or ""
            verdict = analysis.verdict
            judge_tags = None
            if verdict != "AC":
                # 画像用户画像和error_mode 指标
                judge_tags = judge_failure_to_tags(verdict)
            fire_and_forget_error_mode_analysis(
                state.session_id,
                topic=topic,
                description=description,
                final_code=code,
                verdict=verdict,
                judge_failure_tags=judge_tags,
            )
        except Exception:
            logger.warning("error-mode fire-and-forget hook failed (non-fatal)", exc_info=True)

    # ── 运行（sample scope）：写诊断 last_run_results，必要时给轻提示 ──
    if state.judge_scope == "sample":
        update["last_run_results"] = _to_run_results(raw_results, test_cases)
        if analysis.verdict == "AC":
            # 微决策 3：样例全过 → 鼓励提交完整用例
            hint = "\n\n💡 样例都过了，点「提交」跑完整用例试试吧！"
            update["warm_feedback"] = analysis.warm_feedback + hint
            update["tutor_messages"] = update["tutor_messages"][:-1] + [
                {"role": "tutor", "content": feedback_msg + hint}
            ]

    # ── 分 tag 画像（v2）：仅在 full + AC 经 update_profile_node 单写者通道落库 ──
    # 运行（is_run）或 sample 一律不写画像（微决策 1，解读 X）。
    if analysis.verdict == "AC" and not is_run and state.judge_scope != "sample":
        update["profile_delta"] = {
            "tag_primary": state.problem.tag_primary,
            "prob_elo": state.problem.prob_elo,
            "outcome": analysis.verdict,
            "fingerprints": [],
        }
        # AC 收尾状态在这里一次写齐：后续链路为 update_profile_node → critic_node
        # （AC 分支：flush problem_history + phase=reviewing + 暂停在 wait_for_submit）。
        # 与常规 judge 保持一致：只有最终 AC 才记录（WA 重试不写画像）。
        update["status"] = "done"
        logger.info("full+AC — routing to update_profile_node → critic_node")
    elif analysis.verdict != "AC":
        # ── WA：直接回 agent_tutor_node 给重试指导（不写画像）──
        update["status"] = "tutoring"
        logger.info("Not AC — routing to agent_tutor_node for retry guidance")
    else:
        # ── sample + AC（运行）：不写画像、不 done，回 wait_for_submit 保持循环 ──
        update["status"] = "awaiting_submit"
        logger.info("sample+AC (运行) — routing to wait_for_submit_node (no profile, no done)")


def agent_judge_node(state: SessionState) -> dict:
    """LangGraph node: LLM-driven judging via Judge0.

    Reads ``state.judge_scope`` ('sample' for 运行, 'full' for 提交) to pick
    visible vs full test cases. On sample scope, writes diagnostic
    ``last_run_results`` only — never writes profile. Routes via the graph
    conditional edge ``agent_judge_router``:
      - WA                 → agent_tutor_node
      - full + AC          → update_profile_node (writes v2 profile, then done)
      - sample + AC / 运行 → wait_for_submit_node (no profile, no done)

    Args:
        state: Session state with submissions, problem, and judge_scope.

    Returns:
        Plain state update dict (no Command); routing handled by the graph.
    """
    logger.info("▶ agent_judge_node() — cycle=%d scope=%s", state.judge_cycle + 1, state.judge_scope)

    # ── 校验输入 + 加载 problem/test_cases（任一前置不满足返回错误 update）──
    resolved = _resolve_inputs(state)
    if isinstance(resolved, dict):
        return resolved
    code, is_run, problem_dict, test_cases = resolved

    # ── Run test cases via Judge0 (or local fallback) ──
    func_sig = _resolve_function_signature(problem_dict)
    raw_results = run_solution(code, test_cases, timeout=AGENT_JUDGE_TIMEOUT, function_signature=func_sig)
    logger.info("Judge0 returned %d results", len(raw_results))

    # ── 权威 verdict：永远以执行引擎客观结果为准（不信任 LLM 的主观判断） ──
    deterministic_verdict = _deterministic_verdict(raw_results)

    # 仅真实「提交」(is_run=False) 才写 judge_results；运行(is_run=True) 只更新
    # last_run_results（下方 _apply_side_effects）。否则运行结果会被 chat 误读成「提交判题 AC」，
    # 导致用户只点了运行、未提交，却收到「恭喜你 AC」的误导（2026-08-13 实测）。
    # 注意：submissions 通道是 operator.add reducer——绝不能把完整列表放进 update 返回，
    # 只能原地 append（checkpointer 在步末按通道当前值落库），否则提交数翻倍。
    if state.submissions and not is_run:
        state.submissions[-1].judge_results.append(_build_base_result(raw_results, test_cases))

    # ── 判题分析（sample 跳过 LLM / full 走 LLM）──
    analysis = _run_analysis(state, problem_dict, code, raw_results, deterministic_verdict)
    logger.info("Verdict: %s | should_retry=%s", analysis.verdict, analysis.should_retry)

    # ── Build the tutor message with warm feedback ──
    feedback_msg = analysis.warm_feedback
    if analysis.repair_suggestion:
        feedback_msg += f"\n\n**修复建议**\n{analysis.repair_suggestion}"

    # ── Update state ──
    # 注意：不返回 "submissions" —— 该通道是 operator.add reducer，返回完整列表
    # 会把整个列表再追加一遍导致提交数翻倍；judge_results 已在上方原地 append。
    update: dict[str, Any] = {
        "last_verdict": analysis.verdict,
        "warm_feedback": analysis.warm_feedback,
        "repair_suggestion": analysis.repair_suggestion,
        "judge_cycle": state.judge_cycle + 1,
        "tutor_messages": state.tutor_messages
        + [{"role": "tutor", "content": feedback_msg}],
    }

    _apply_side_effects(state, analysis, raw_results, test_cases, is_run, feedback_msg, update, code=code)
    return update
