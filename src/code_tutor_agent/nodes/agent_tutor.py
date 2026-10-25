"""Agent 辅导节点 — 判题非 AC 后生成重试指导的 LangGraph 节点。

职责（2026-10-25 归位）：判题只**交棒**客观执行结果（``state.last_exec_results``），
本节点读它生成 ``warm_feedback`` / ``repair_suggestion`` 并追加 ``tutor_messages``。
verdict 的权威永远属于执行引擎与判题节点——本节点只读 ``last_verdict`` 作为
``forced_verdict`` 锁死 LLM，绝不改判、绝不重跑用例。

AC 线不经过本节点（``agent_judge_router`` 直接路由到 update_profile_node /
wait_for_submit_node），所以 AC 的「祝贺 + 复杂度分析」目前仍由判题节点生成。

节点出口仍是确定性单跳：返回纯 dict，靠静态边
``agent_tutor_node → wait_for_submit_node`` 回到等待提交（改 Command(goto) 会与
静态边共存导致双节点执行，见 2026-08-04 历史坑）。
"""

from __future__ import annotations

import logging

from code_tutor_agent.agents.agent_judge import analyze_judge_results
from code_tutor_agent.sandbox.runner import RunnerResult
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# LLM 调用彻底失败时的兜底文案（与判题侧降级口径一致：只降文案质量，不降 verdict）
_FALLBACK_WARM = "你的代码已经跑起来了，但还有一些用例没通过。别灰心，看看具体哪里出了问题 👀"


def _rebuild_results(plain: list[dict]) -> list[RunnerResult]:
    """交棒载荷 → RunnerResult，供 ``format_results_for_prompt`` 逐用例渲染。

    必须保留每个用例的期望/实际输出：LLM 看不到实际输出时会臆造「你的输出」。
    """
    return [
        RunnerResult(
            test_case_id=int(d.get("test_case_id", 0) or 0),
            status=d.get("status", "") or "",
            detail=d.get("detail", "") or "",
            runtime_ms=float(d.get("runtime_ms", 0.0) or 0.0),
            memory_kb=float(d.get("memory_kb", 0.0) or 0.0),
            expected_output=d.get("expected_output", "") or "",
            actual_output=d.get("actual_output", "") or "",
        )
        for d in (plain or [])
    ]


def agent_tutor_node(state: SessionState) -> dict:
    """Generate retry guidance after a non-AC judge cycle, then await resubmission.

    Args:
        state: Session state carrying ``last_verdict`` and ``last_exec_results``
            written by ``agent_judge_node``.

    Returns:
        Plain state update (no Command); graph routes via the static
        edge ``agent_tutor_node → wait_for_submit_node``.
    """
    verdict = state.last_verdict or ""
    cycle = state.judge_cycle
    logger.info(
        "▶ agent_tutor_node() — verdict=%s cycle=%d scope=%s",
        verdict, cycle, state.judge_scope,
    )

    results = _rebuild_results(state.last_exec_results)
    code = state.submissions[-1].code if state.submissions else ""
    problem = state.problem

    if state.judge_scope == "sample":
        # 运行=快速自测，绝不为一句反馈等 LLM（与判题侧 sample 同口径：
        # 历史上运行曾误入 LLM，单次耗时 14~85s）
        passed = sum(1 for r in results if r.status == "Passed")
        warm = (
            f"样例用例 {passed}/{len(results)} 通过，还有问题，继续调试～"
            if results
            else "没取到运行结果，麻烦再点一次「运行」试试。"
        )
        repair = ""
    elif not results:
        logger.warning("agent_tutor_node — 交棒载荷为空，降级为确定性提示")
        warm = "没取到判题结果，麻烦重新提交一次试试。"
        repair = ""
    else:
        try:
            # purpose 沿用 "judge"：换宿主不换模型/成本桶，避免静默改成本口径
            analysis = analyze_judge_results(
                code=code,
                title=problem.title if problem else "",
                difficulty=(problem.difficulty if problem else "") or state.difficulty,
                topic=(problem.topic if problem else "") or state.topic,
                description=problem.description if problem else "",
                results=results,
                forced_verdict=verdict,
            )
            warm = analysis.warm_feedback
            repair = analysis.repair_suggestion
        except Exception as exc:  # noqa: BLE001
            # analyze_judge_results 内部已降级，走到这里说明网关层彻底不可用。
            # 抛异常会击穿整次 invoke：状态不落盘、会话失去挂起节点，
            # 此后 /run 一律 400（2026-09-04 判题侧同类事故），故只降文案不降流程。
            logger.warning("辅导文案生成失败（非致命，用确定性兜底）: %s", exc)
            warm, repair = _FALLBACK_WARM, ""

    feedback_msg = warm
    if repair:
        feedback_msg += f"\n\n**修复建议**\n{repair}"

    return {
        # 未通过 → 循环等待用户修改（AC 不在此分支）
        "status": "awaiting_submit",
        "warm_feedback": warm,
        "repair_suggestion": repair,
        "tutor_messages": list(state.tutor_messages)
        + [{"role": "tutor", "content": feedback_msg}],
    }
