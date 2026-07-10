"""判题节点 — 多阶段判题（D3）

三阶段判题流程（PRD §3.2）：

    Phase 1 · 基础判题
        跑出题 Agent 预设的测试用例。
        有挂（WA/RE）→ 直接交辅导 Agent，不浪费 token 跑对抗。

    Phase 2 · 对抗测试（AC 后自动触发）
        2a. 代码弱点分析（AST 启发式，零 LLM）
        2b. 边界对抗（纯规则枚举：空 / 单元素 / 极值 / 重复 / 负数）
        2c. 规模对抗（LLM 出分布特征 + 规则拼大规模数组）
        任何一步挂了 → 交辅导（带着对抗失败原因）。

    Phase 3 · 多维评审（全部通过后触发）
        LLM 做复杂度估算 + 风格点评。
        评审不拦 AC — 评审只影响「下一题难度」和「用户画像」。
        见 PRD §3.2 Phase 4 说明。

    面试考点：为什么这么设计？
        - 教学效率：基础挂了说明用户还没理解题，对抗没有意义
        - 渐进式反馈：WA → 辅导 L1~L4；AC+对抗挂 → "你的解法在大规模下撑不住"
        - 评审不拦 AC 原则：代码对了就该给正向反馈，评审结果影响后续规划
"""

from __future__ import annotations

import logging

from langgraph.types import Command

from code_tutor_agent.db.database import get_problem_by_id
from code_tutor_agent.sandbox.adversarial import (
    AdversarialSuite,
    run_adversarial_suite,
)
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import JudgeResult, SessionState, Submission

logger = logging.getLogger(__name__)

# ── 基础判题超时（用户不可信代码，比参考解更严格） ──
BASE_TIMEOUT = 5.0


def judge_node(state: SessionState) -> Command:
    """Run the full three-phase judge pipeline.

    Args:
        state: Session state with at least one pending submission.

    Returns:
        Command routing to ``tutor_node`` (always — judge never goes
        anywhere else; the tutor decides what hint level to give based
        on the verdict).
    """
    logger.info("▶ judge_node()")
    if not state.submissions:
        logger.debug("Returning Command with goto=%s", 'return Command(')
        return Command(
            update={"error_message": "No submission to judge", "status": "error"},
            goto="__end__",
        )

    last_sub: Submission = state.submissions[-1]
    problem_id = state.problem.problem_id if state.problem else 0

    # ── Load problem from DB ──
    problem_dict = get_problem_by_id(problem_id) if problem_id else None
    if not problem_dict:
        logger.warning("Problem %d not in DB; cannot judge", problem_id)
        logger.debug("Returning Command with goto=%s", 'return Command(')
        return Command(
            update={"error_message": f"Problem {problem_id} not found", "status": "error"},
            goto="__end__",
        )

    test_cases = problem_dict.test_cases

    # ════════════════════════════════════════════
    #  Phase 1: 基础判题
    # ════════════════════════════════════════════
    logger.info("Phase 1 — base judging (%d test cases)", len(test_cases))
    base_results = run_solution(last_sub.code, test_cases, timeout=BASE_TIMEOUT)

    base_verdict = _collapse_verdict(base_results)
    base_result = JudgeResult(
        status=base_verdict,
        phase="base",
        detail=next(
            (r.detail for r in base_results if r.status != "Passed"),
            f"{sum(1 for r in base_results if r.status == 'Passed')}/{len(base_results)} passed",
        ),
        runtime_ms=sum(r.runtime_ms for r in base_results),
    )
    last_sub.judge_results.append(base_result)
    logger.info("Phase 1 → %s (%d/%d)", base_verdict,
                sum(1 for r in base_results if r.status == "Passed"), len(base_results))

    # Phase 1 failed → 直接交辅导（不等后台用例生成）
    if base_verdict != "AC":
        return _route_to_tutor(state, last_sub, base_verdict, "base_fail")

    # ── Phase 1 AC，但测试用例数太少 → 等后台用例生成完再跑一次 ──
    MIN_TC_FOR_FULL = 5  # <5 说明后台还没跑完（初始只有 2 个 sample）
    if len(test_cases) < MIN_TC_FOR_FULL:
        logger.info("Only %d test cases — waiting for background gen...", len(test_cases))
        import time as _time
        for _ in range(20):  # poll up to 20s
            _time.sleep(1)
            problem_dict = get_problem_by_id(problem_id)
            test_cases = problem_dict.test_cases
            if len(test_cases) >= MIN_TC_FOR_FULL:
                break

        if len(test_cases) >= MIN_TC_FOR_FULL:
            logger.info("Background gen done — re-running Phase 1 with %d test cases", len(test_cases))
            base_results = run_solution(last_sub.code, test_cases, timeout=BASE_TIMEOUT)
            base_verdict = _collapse_verdict(base_results)
            # Replace the judge result with new results from full suite
            last_sub.judge_results[-1] = JudgeResult(
                status=base_verdict,
                phase="base",
                detail=next(
                    (r.detail for r in base_results if r.status != "Passed"),
                    f"{sum(1 for r in base_results if r.status == 'Passed')}/{len(base_results)} passed",
                ),
                runtime_ms=sum(r.runtime_ms for r in base_results),
            )
            if base_verdict != "AC":
                logger.info("Full-suite Phase 1 → %s — routing to tutor", base_verdict)
                return _route_to_tutor(state, last_sub, base_verdict, "base_fail")

        logger.info("Phase 1 AC — proceeding to Phase 2 (%d test cases)", len(test_cases))

    # ════════════════════════════════════════════
    #  Phase 2: 对抗测试（只有基础 AC 才跑）
    # ════════════════════════════════════════════
    logger.info("Phase 2 — adversarial suite")
    adv_suite = run_adversarial_suite(problem_dict, last_sub.code)

    # ── 记录对抗结果
    for r in adv_suite.boundary_results:
        last_sub.judge_results.append(JudgeResult(
            status=_normalise_verdict(r.status),
            phase="adversarial_boundary",
            detail=r.detail,
            runtime_ms=r.runtime_ms,
        ))

    for r in adv_suite.scale_results:
        last_sub.judge_results.append(JudgeResult(
            status=_normalise_verdict(r.status),
            phase="adversarial_scale",
            detail=r.detail,
            runtime_ms=r.runtime_ms,
        ))

    if adv_suite.has_any_failure():
        fail_reason = _describe_adversarial_failure(adv_suite)
        logger.warning("Phase 2 → adversarial failed: %s", fail_reason)
        return _route_to_tutor(state, last_sub, "AC", fail_reason)

    logger.info("Phase 2 → all adversarial passed")

    # ════════════════════════════════════════════
    #  Phase 3: 多维评审（全部通过后触发）
    # ════════════════════════════════════════════
    if adv_suite.review:
        logger.info("Phase 3 — review: %s", adv_suite.review.get("summary", ""))
        last_sub.judge_results.append(JudgeResult(
            status="AC", phase="review",
            detail=str(adv_suite.review),
        ))

    # 全部通过 → 带着评审结果去 tutor（tutor 会给出 AC 正向反馈）
    return _route_to_tutor(state, last_sub, "AC", "all_passed", adv_suite.review)


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


def _normalise_verdict(status: str) -> str:
    """Map RunnerResult status values to JudgeResult literal values.

    RunnerResult returns human-readable status like "Runtime Error",
    "Wrong Answer", "Passed".  JudgeResult accepts the short forms:
    "AC", "WA", "TLE", "RE", "CE".
    """
    mapping = {
        "Passed": "AC",
        "Wrong Answer": "WA",
        "Runtime Error": "RE",
        "TLE": "TLE",
        "Time Limit Exceeded": "TLE",
    }
    return mapping.get(status, "RE")


def _collapse_verdict(results: list) -> str:
    """将一组 RunnerResult 归约为单个 verdict。

    优先级：TLE > RE > WA > AC。
    RunnerResult 返回 "Passed" 而非 "AC"，需映射。
    """
    tle = any(r.status == "TLE" for r in results)
    re_err = any(r.status == "Runtime Error" for r in results)
    wa = any(r.status == "Wrong Answer" for r in results)
    all_pass = all(r.status == "Passed" for r in results)

    if tle:
        return "TLE"
    if re_err:
        return "RE"
    if wa:
        return "WA"
    if all_pass:
        return "AC"
    return "WA"  # fallback


def _route_to_tutor(
    state: SessionState,
    submission: Submission,
    verdict: str,
    trigger: str,
    review: dict | None = None,
) -> Command:
    """统一路由到 tutor_node，附带判题上下文。

    设置 ``last_verdict`` 和 ``adversarial_triggered`` 供
    tutor_node 决策 hint_level 使用。
    """
    # Record verdict on the submission itself (for submission history display)
    submission.verdict = verdict

    # 记录 hint_level_given（会在 tutor 中更新）
    update = {
        "submissions": state.submissions,
        "last_verdict": verdict,
        "adversarial_triggered": verdict == "AC" and trigger != "base_fail",
        "status": "tutoring",
    }

    if review:
        update["last_review_payload"] = review

    # ── 更新用户画像 ──
    try:
        from code_tutor_agent.db.database import update_profile_on_result
        topic = state.problem.topic if state.problem else "未知"
        update_profile_on_result(topic=topic, verdict=verdict)
    except Exception:
        logger.warning("Profile update failed (non-fatal)", exc_info=True)

    logger.info("Route to tutor → verdict=%s trigger=%s", verdict, trigger)
    logger.debug("Returning Command with goto=%s", 'return Command(update=update, goto="tutor_node")')
    return Command(update=update, goto="tutor_node")


def _describe_adversarial_failure(suite: AdversarialSuite) -> str:
    """生成对抗失败的人类可读描述，供 tutor_node 使用。

    例如：「你的代码在小规模边界用例（负数/重复）上出错了」
    或：「你的代码在 10⁵ 规模下超时了，复杂度可能偏高」
    """
    parts = []
    if suite.failed_boundary:
        failed = suite.failed_boundary
        details = [f"tc#{r.test_case_id}: {r.detail[:60]}" for r in failed[:3]]
        parts.append(f"边界对抗挂了 ({len(failed)} 个): {'; '.join(details)}")
    if suite.failed_scale:
        failed = suite.failed_scale
        parts.append(f"规模对抗挂了: {failed[0].detail[:80]}")
    return " | ".join(parts) if parts else "对抗测试未全部通过"