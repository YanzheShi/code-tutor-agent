"""后台用例生成 — 随机 + 边界 + 可见用例校验（设计 §5「复用 _generate_complex_tests 逻辑」）。

逻辑镜像 ``api/services/generation.py::_generate_complex_tests``（现状快速路径），
但全部依赖走 generation/ 包的 Gateways，包内不触碰 API 层 / SessionState。

签名约定：``build_suite(agent, problem_id, sink)`` 为纯同步阻塞函数，
调用方必须用 ``asyncio.to_thread``（或线程）执行，切勿在事件循环内直接调用。

结构（重构后）：
- ``build_suite``        编排层：取题 → 计算公共参数 → 驱动两个生成分支 + 可见用例校验 → 回写 DB。
- ``_generate_random_cases``     随机生成分支：调用沙箱随机输入，逐个验证后产出用例。
- ``_generate_llm_boundary_cases``  LM 生成分支：调用 LLM 生成边界用例，逐个验证后产出用例。
- ``_verify_visible_cases``       校验既有可见用例（用参考解重算期望，跑挂的丢弃）。
- ``_validate_against_reference`` 交叉验证 / 单解验证的共用核心（被以上三个分支复用）。
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


def _validate_against_reference(
    agent: ProblemGenerationAgent,
    tc: dict,
    *,
    func_sig: str,
    use_cross_validation: bool,
    optimal_code: str,
    brute_code: str,
    ref_code: str,
) -> tuple[str | None, bool]:
    """用参考解验证单个用例，返回 ``(期望输出, 是否交叉不一致)``。

    - 双解交叉模式：两条参考解各跑一遍，无返回 / 参考解异常 / 结果不一致均判为丢弃；
      仅「结果不一致」时第二个返回值为 ``True``（用于 dropped_cross 计数）。
    - 单解模式：仅用最优或暴力解跑一遍，无返回 / 异常 / 无输出即丢弃。

    第一个返回值为 ``None`` 即该用例应丢弃；否则为算出的期望输出。
    """
    if use_cross_validation:
        opt_results = agent.sandbox.run_solution(
            optimal_code, [tc], function_signature=func_sig,
        )
        brute_results = agent.sandbox.run_solution(
            brute_code, [tc], function_signature=func_sig,
        )
        if not opt_results or not brute_results:
            return None, False
        opt_r, brute_r = opt_results[0], brute_results[0]
        if opt_r.status in _DROP_STATUSES or brute_r.status in _DROP_STATUSES:
            return None, False
        opt_actual = (opt_r.detail or "").strip()
        brute_actual = (brute_r.detail or "").strip()
        if not opt_actual or not brute_actual or opt_actual != brute_actual:
            return None, True
        return opt_actual, False

    results = agent.sandbox.run_solution(ref_code, [tc], function_signature=func_sig)
    if not results:
        return None, False
    r = results[0]
    if r.status in _DROP_STATUSES:
        return None, False
    actual = r.detail or ""
    if not actual:
        return None, False
    return actual, False


def _generate_random_cases(
    agent: ProblemGenerationAgent,
    sink: ProgressSink,
    *,
    func_sig: str,
    sort_inputs: bool,
    use_cross_validation: bool,
    optimal_code: str,
    brute_code: str,
    ref_code: str,
    random_inputs: list,
    problem_id: int,
) -> tuple[list[dict], int]:
    """随机生成分支：对沙箱随机输入逐个对齐签名并验证，返回 ``(用例列表, 交叉丢弃数)``。"""
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
        expected, cross_mismatch = _validate_against_reference(
            agent, tc,
            func_sig=func_sig, use_cross_validation=use_cross_validation,
            optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
        )
        if expected is None:
            # 随机分支：任何交叉验证失败都计入丢弃（对齐原逻辑的三个分支）
            if use_cross_validation:
                dropped_cross += 1
            continue
        tc["expected_output"] = expected
        all_tcs.append(tc)
    return all_tcs, dropped_cross


def _generate_llm_boundary_cases(
    agent: ProblemGenerationAgent,
    sink: ProgressSink,
    *,
    func_sig: str,
    sort_inputs: bool,
    use_cross_validation: bool,
    optimal_code: str,
    brute_code: str,
    ref_code: str,
    full,
    random_visible_examples: list,
    problem_id: int,
) -> tuple[list[dict], int]:
    """LM 生成分支：调用 LLM 生成边界用例并逐个验证，返回 ``(用例列表, 交叉丢弃数)``。"""
    sink.event(GenEvent("progress", "🤖 正在生成边界测试用例..."))
    all_tcs: list[dict] = []
    dropped_cross = 0
    try:
        boundary_cases = agent.llm.generate_boundary(
            title=getattr(full, "title", "") or "",
            description=getattr(full, "description", "") or "",
            difficulty=getattr(full, "difficulty", "medium") or "medium",
            function_signature=func_sig,
            constraints=getattr(full, "constraints", None) or [],
            optimal_code=ref_code,
            existing_cases=random_visible_examples,
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
            expected, cross_mismatch = _validate_against_reference(
                agent, _tc,
                func_sig=func_sig, use_cross_validation=use_cross_validation,
                optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
            )
            if expected is None:
                # 边界分支：仅「双解结果不一致」计入交叉丢弃（对齐原逻辑）
                if use_cross_validation and cross_mismatch:
                    dropped_cross += 1
                continue
            bc["expected_output"] = expected
            bc["is_hidden"] = True
            bc["explanation"] = bc.get("explanation", "LLM 生成的边界用例")
            all_tcs.append(bc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prompt B (boundary LLM) failed: %s", exc)
    return all_tcs, dropped_cross


def _verify_visible_cases(
    agent: ProblemGenerationAgent,
    *,
    func_sig: str,
    sort_inputs: bool,
    use_cross_validation: bool,
    optimal_code: str,
    brute_code: str,
    ref_code: str,
    full,
    problem_id: int,
) -> tuple[list[dict], int]:
    """校验既有可见用例（用参考解重算期望，跑挂的丢弃），返回 ``(用例列表, 交叉丢弃数)``。"""
    existing_tcs = getattr(full, "test_cases", None) or []
    sample_tcs = [tc for tc in existing_tcs if not tc.get("is_hidden", False)][:2]
    verified_visible: list[dict] = []
    dropped_cross = 0
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
        expected, cross_mismatch = _validate_against_reference(
            agent, _tc,
            func_sig=func_sig, use_cross_validation=use_cross_validation,
            optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
        )
        if expected is None:
            # 可见分支：仅「双解结果不一致」计入交叉丢弃（对齐原逻辑）
            if use_cross_validation and cross_mismatch:
                dropped_cross += 1
            continue
        san["expected_output"] = expected
        san["is_hidden"] = False
        verified_visible.append(san)
    return verified_visible, dropped_cross


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
    # 根据提示和参数情况生成随机测试用例
    random_inputs = agent.sandbox.random_inputs(
        func_sig, n=_RANDOM_COUNT, seed=problem_id,
        constraints=constraints, description=description,
    )
    if not random_inputs:
        sink.event(GenEvent("progress", "✅ 无函数签名，跳过随机测试生成"))
        return

    # ── 随机生成分支 ──
    random_cases, dropped_random = _generate_random_cases(
        agent, sink,
        func_sig=func_sig, sort_inputs=sort_inputs,
        use_cross_validation=use_cross_validation,
        optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
        random_inputs=random_inputs, problem_id=problem_id,
    )

    # ── LM 生成分支（边界用例）──
    boundary_cases, dropped_boundary = _generate_llm_boundary_cases(
        agent, sink,
        func_sig=func_sig, sort_inputs=sort_inputs,
        use_cross_validation=use_cross_validation,
        optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
        full=full, random_visible_examples=random_cases[:4], problem_id=problem_id,
    )

    # ── 既有可见用例校验 ──
    verified_visible, dropped_visible = _verify_visible_cases(
        agent,
        func_sig=func_sig, sort_inputs=sort_inputs,
        use_cross_validation=use_cross_validation,
        optimal_code=optimal_code, brute_code=brute_code, ref_code=ref_code,
        full=full, problem_id=problem_id,
    )

    dropped_cross = dropped_random + dropped_boundary + dropped_visible
    full_suite = verified_visible + random_cases + boundary_cases
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
