"""后台用例生成 — 随机 + 边界 + 可见用例校验（设计 §5「复用 _generate_complex_tests 逻辑」）。

逻辑镜像 ``api/services/generation.py::_generate_complex_tests``（现状快速路径），
但全部依赖走 generation/ 包的 Gateways，包内不触碰 API 层 / SessionState。

签名约定：``build_suite(agent, problem_id, sink)`` 为纯同步阻塞函数，
调用方必须用 ``asyncio.to_thread``（或线程）执行，切勿在事件循环内直接调用。
"""

from __future__ import annotations

import logging

from code_tutor_agent.generation.problem_generation_agent import ProblemGenerationAgent
from code_tutor_agent.generation.state import GenEvent, ProgressSink

logger = logging.getLogger(__name__)

# 参考解在这些状态下说明 input 本身有问题（或参考解崩了），该用例应丢弃
_DROP_STATUSES = {"Runtime Error", "TLE", "Judge Error"}

_RANDOM_COUNT = 12
_BOUNDARY_COUNT = 8


def build_suite(agent: ProblemGenerationAgent, problem_id: int, sink: ProgressSink) -> None:
    """生成完整测试套件并回写 DB（可见：已验证示例 + 随机兜底，至多 4 条）。
    参考解缺失时静默跳过（后台补全契约，不抛错）。"""
    full = agent.store.get_problem(problem_id)
    if full is None:
        logger.warning("Problem %d not found", problem_id)
        return

    optimal_code = (getattr(full, "optimal_solution", "") or "").strip()
    brute_code = (getattr(full, "brute_solution", "") or "").strip()
    func_sig = getattr(full, "function_signature", "") or ""
    ref_code = optimal_code or brute_code
    use_cross_validation = bool(optimal_code and brute_code)

    if not ref_code:
        logger.warning("No optimal/brute solution for %d — skipping bg test gen", problem_id)
        sink.event(GenEvent("info", "📝 无参考代码，跳过后台测试生成"))
        return

    sink.event(GenEvent(
        "progress", "🔄 双参考解交叉验证" if use_cross_validation else "🔄 单参考解验证",
    ))

    constraints = getattr(full, "constraints", None) or []
    description = getattr(full, "description", "") or ""
    sort_inputs = agent.sandbox.needs_sorted_inputs(*constraints, description)

    sink.event(GenEvent("progress", "🧪 正在生成更多测试用例..."))
    random_inputs = agent.sandbox.random_inputs(
        func_sig, n=_RANDOM_COUNT, seed=problem_id,
        constraints=constraints, description=description,
    )
    if not random_inputs:
        sink.event(GenEvent("progress", "✅ 无函数签名，跳过随机测试生成"))
        return

    sink.event(GenEvent("progress", f"🔧 正在运行参考解验证 {len(random_inputs)} 个用例..."))
    all_tcs: list[dict] = []
    dropped_cross = 0
    for idx, inp in enumerate(random_inputs):
        if func_sig:
            # 随机用例生成后需与签名对齐（list/int/bool 参数转换）
            san = agent.sandbox.sanitize(func_sig, {"input_args": inp}, sort_inputs=sort_inputs)
            if not san or not san.get("input_args"):
                logger.warning(
                    "Random case input malformed, dropping (pid=%d idx=%d)",
                    problem_id, idx,
                )
                continue
            inp = san["input_args"]
        tc = {
            "input_args": inp,
            "expected_output": "",
            "is_hidden": idx >= 4,
            "explanation": f"随机生成测试 {idx + 1}",
        }
        # 交叉验证用两条参考解分别跑同一用例，任何一条无结果即弃
        if use_cross_validation:
            opt_results = agent.sandbox.run_solution(
                optimal_code, [tc], function_signature=func_sig,
            )
            brute_results = agent.sandbox.run_solution(
                brute_code, [tc], function_signature=func_sig,
            )
            if not opt_results or not brute_results:
                logger.warning(
                    "Cross-validation: no results, dropping (pid=%d idx=%d)",
                    problem_id, idx,
                )
                dropped_cross += 1
                continue
            opt_r, brute_r = opt_results[0], brute_results[0]
            if opt_r.status in _DROP_STATUSES or brute_r.status in _DROP_STATUSES:
                dropped_cross += 1
                continue
            opt_actual = (opt_r.detail or "").strip()
            brute_actual = (brute_r.detail or "").strip()
            if not opt_actual or not brute_actual or opt_actual != brute_actual:
                dropped_cross += 1
                continue
            tc["expected_output"] = opt_actual
            all_tcs.append(tc)
        else:
            results = agent.sandbox.run_solution(ref_code, [tc], function_signature=func_sig)
            if not results:
                continue
            r = results[0]
            if r.status in _DROP_STATUSES:
                continue
            actual = r.detail or ""
            if actual:
                tc["expected_output"] = actual
                all_tcs.append(tc)

    # ── LLM 边界用例 ──
    sink.event(GenEvent("progress", "🤖 正在生成边界测试用例..."))
    try:
        boundary_cases = agent.llm.generate_boundary(
            title=getattr(full, "title", "") or "",
            description=description,
            difficulty=getattr(full, "difficulty", "medium") or "medium",
            function_signature=func_sig,
            constraints=constraints,
            optimal_code=ref_code,
            existing_cases=all_tcs[:4],
            count=_BOUNDARY_COUNT,
        ) or []
        sink.event(GenEvent("progress", f"🔧 正在验证 {len(boundary_cases)} 个边界用例..."))
        for bc in boundary_cases:
            if func_sig:
                bc = agent.sandbox.sanitize(func_sig, bc, sort_inputs=sort_inputs) or {}
                if not bc.get("input_args"):
                    logger.warning(
                        "Boundary case input malformed, dropping: %s",
                        bc.get("explanation", ""),
                    )
                    continue
            _tc = {"input_args": bc.get("input_args", []), "expected_output": ""}
            if use_cross_validation:
                opt_results = agent.sandbox.run_solution(
                    optimal_code, [_tc], function_signature=func_sig,
                )
                brute_results = agent.sandbox.run_solution(
                    brute_code, [_tc], function_signature=func_sig,
                )
                if not opt_results or not brute_results:
                    continue
                opt_r, brute_r = opt_results[0], brute_results[0]
                if opt_r.status in _DROP_STATUSES or brute_r.status in _DROP_STATUSES:
                    continue
                opt_actual = (opt_r.detail or "").strip()
                brute_actual = (brute_r.detail or "").strip()
                if not opt_actual or not brute_actual or opt_actual != brute_actual:
                    dropped_cross += 1
                    continue
                bc["expected_output"] = opt_actual
            else:
                results = agent.sandbox.run_solution(ref_code, [_tc], function_signature=func_sig)
                if not results:
                    continue
                r = results[0]
                if r.status in _DROP_STATUSES:
                    continue
                actual = r.detail or ""
                if not actual:
                    continue
                bc["expected_output"] = actual
            bc["is_hidden"] = True
            bc["explanation"] = bc.get("explanation", "LLM 生成的边界用例")
            all_tcs.append(bc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prompt B (boundary LLM) failed: %s", exc)

    # ── 验证 LLM 生成的示例/可见用例（用参考解重算期望，跑挂的丢弃）──
    existing_tcs = getattr(full, "test_cases", None) or []
    sample_tcs = [tc for tc in existing_tcs if not tc.get("is_hidden", False)][:2]
    verified_visible: list[dict] = []
    for tc in sample_tcs:
        san = dict(tc)
        if func_sig:
            san = agent.sandbox.sanitize(func_sig, san, sort_inputs=sort_inputs) or {}
        if not san or not san.get("input_args"):
            logger.warning(
                "Sample/visible case input malformed, dropping: %s",
                tc.get("explanation", ""),
            )
            continue
        _tc = {"input_args": san["input_args"], "expected_output": ""}
        if use_cross_validation:
            opt_results = agent.sandbox.run_solution(
                optimal_code, [_tc], function_signature=func_sig,
            )
            brute_results = agent.sandbox.run_solution(
                brute_code, [_tc], function_signature=func_sig,
            )
            if not opt_results or not brute_results:
                continue
            opt_r, brute_r = opt_results[0], brute_results[0]
            if opt_r.status in _DROP_STATUSES or brute_r.status in _DROP_STATUSES:
                continue
            opt_actual = (opt_r.detail or "").strip()
            brute_actual = (brute_r.detail or "").strip()
            if not opt_actual or not brute_actual or opt_actual != brute_actual:
                dropped_cross += 1
                continue
            san["expected_output"] = opt_actual
        else:
            results = agent.sandbox.run_solution(ref_code, [_tc], function_signature=func_sig)
            if not results:
                continue
            r = results[0]
            if r.status in _DROP_STATUSES:
                continue
            actual = r.detail or ""
            if not actual:
                continue
            san["expected_output"] = actual
        san["is_hidden"] = False
        verified_visible.append(san)

    full_suite = verified_visible + all_tcs
    visible_final = [tc for tc in full_suite if not tc.get("is_hidden", False)][:4]

    try:
        agent.store.update_test_cases(problem_id, full_suite, visible_final)
    except Exception as exc:  # noqa: BLE001
        logger.error("update_test_cases(%d) failed: %s", problem_id, exc)
        raise

    drop_msg = f"（交叉验证丢弃 {dropped_cross} 个不一致用例）" if dropped_cross else ""
    sink.event(GenEvent(
        "progress",
        f"✅ 共 {len(full_suite)} 个测试用例已就绪{drop_msg}（含 LLM 边界用例）",
    ))
    logger.info(
        "Completed background test gen for problem %d (dropped %d cross-validation)",
        problem_id, dropped_cross,
    )
