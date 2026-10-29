"""判题后在后台补写辅导文案（/submit 拆链路，2026-10-27）。

一次提交的耗时由两段组成：执行引擎判题（有界，最坏约 20s）和 LLM 点评（此前**没有
任何超时上界**，且串在判题后面、同在一条同步 POST 里）——于是用户点一次提交可能白屏
等两分钟。现在 /submit 跑完判题就立即返回 verdict + 逐用例结果，本模块负责在后台把
点评生成出来写回同一个会话线程，前端轮询 /state 取。

写回必须走 ``pause_safe_update``：此刻会话正挂在 ``wait_for_submit_node`` 的
interrupt 上，直接 ``update_state`` 会丢掉挂起中断，之后所有 /submit 的
Command(resume) 都会空转（详见 api/deps.py::pause_safe_update 注释）。

陈旧写入防护：文案生成期间用户可能又提交了一次，或 AC 后直接点了「下一题」
（critic 的 ABANDON 分支会清空 tutor_messages）。所以写回前重读 state 比对
``judge_cycle`` 与 ``problem_id``，任一不匹配就丢弃——**迟到的点评比没有点评更糟**，
它会把上一题的讲解贴到新一题下面。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 事件循环对 task 只持弱引用，不另存一份可能被 GC 掉导致文案永远不落地
_TASKS: set[asyncio.Task] = set()


def _problem_id(values: dict) -> Any:
    """从 checkpoint 值里取当前题 id（problem 可能是 ProblemMeta 也可能是 dict）。"""
    problem = values.get("problem")
    if problem is None:
        return None
    if isinstance(problem, dict):
        return problem.get("problem_id")
    return getattr(problem, "problem_id", None)


def schedule_tutor_feedback(
    graph: Any,
    config: dict,
    *,
    expected_cycle: int,
    expected_problem_id: Any,
) -> asyncio.Task | None:
    """把「生成文案 + 写回」丢进后台，调用方（HTTP handler）立即返回，不等 LLM。

    Args:
        graph: 已编译的 LangGraph。
        config: 该会话的 run config（含 thread_id）。
        expected_cycle: 本次判题结束后的 ``judge_cycle``，写回时必须仍等于它。
        expected_problem_id: 本次判题所属题 id，写回时题目必须未换。

    Returns:
        后台任务；无运行中的事件循环时返回 None（此时本轮没有点评，仅缺文案）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("schedule_tutor_feedback — 无运行中事件循环，本轮辅导文案跳过")
        return None

    task = loop.create_task(
        _run(graph, config, expected_cycle, expected_problem_id)
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def _run(graph: Any, config: dict, expected_cycle: int, expected_problem_id: Any) -> None:
    # 文案生成是阻塞的 LLM 调用（节点全图为同步语义），丢线程池，别占事件循环
    try:
        await asyncio.to_thread(
            _generate_and_write, graph, config, expected_cycle, expected_problem_id
        )
    except Exception:  # noqa: BLE001
        # 判题结果已经回给前端了，这里失败只是少了那段讲解，不能影响任何东西
        logger.exception("deferred tutor feedback failed (non-fatal)")


def _stale_reason(values: dict, expected_cycle: int, expected_problem_id: Any) -> str | None:
    """会话是否已前进到别的一轮/另一道题；是则返回丢弃原因，否返回 None。"""
    if values.get("judge_cycle") != expected_cycle:
        return f"cycle {expected_cycle}→{values.get('judge_cycle')}"
    if not values.get("tutor_deferred"):
        return "tutor_deferred 已清"
    pid = _problem_id(values)
    if pid != expected_problem_id:
        return f"problem {expected_problem_id}→{pid}"
    return None


def _generate_and_write(
    graph: Any, config: dict, expected_cycle: int, expected_problem_id: Any
) -> None:
    from code_tutor_agent.api.deps import pause_safe_update
    from code_tutor_agent.nodes.agent_tutor import build_tutor_update
    from code_tutor_agent.schemas.state import SessionState

    sid = (config.get("configurable") or {}).get("thread_id", "?")
    state = graph.get_state(config)
    values = state.values or {}

    reason = _stale_reason(values, expected_cycle, expected_problem_id)
    if reason:
        logger.info("deferred tutor dropped (session=%s) — 会话已前进: %s", sid, reason)
        return

    # 这里可能耗时数十秒（LLM）。期间用户完全可能再提交一次或点「下一题」，
    # 而写回用的 pause_safe_update 会重新触发 wait_for_submit 的 interrupt ——
    # 拿陈旧轮次的文案去盖新一轮，比不写更糟。所以文案算完后、落库前再校验一次。
    update = build_tutor_update(SessionState(**values))

    values = graph.get_state(config).values or {}
    reason = _stale_reason(values, expected_cycle, expected_problem_id)
    if reason:
        logger.info(
            "deferred tutor discarded after generation (session=%s): %s", sid, reason
        )
        return

    # 只补文案：status/phase 已由判题节点与 critic 定好，这里动它会踩到前端 done 判定
    pause_safe_update(graph, config, {
        "warm_feedback": update["warm_feedback"],
        "repair_suggestion": update["repair_suggestion"],
        "tutor_messages": update["tutor_messages"],
    })
    logger.info(
        "deferred tutor written (session=%s, cycle=%s, verdict=%s)",
        sid, expected_cycle, values.get("last_verdict"),
    )
