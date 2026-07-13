"""Agent 路由节点 — LLM 判题后决定下一跳的 LangGraph 节点。

该节点接收 ``agent_judge_node`` 的输出，根据 verdict 决定下一步路由。
温暖反馈和修复建议已经在判题节点中由 LLM 生成好了；
本节点只负责路由决策。

节点流转：
    agent_judge_node → agent_tutor_node
        ├── AC → planner_node（下一题）
        └── WA → wait_for_submit_node（继续修改）
"""

from __future__ import annotations

import logging

from langgraph.types import Command

from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# ── AC 完成消息池（每次随机选一个） ──
_AC_MESSAGES = [
    "🎉 全部通过！你的代码逻辑正确。",
    "✅ AC！要不要试试不同解法优化一下？",
    "✅ 完美通过！下一题继续加油 💪",
    "🎊 AC 了！可以去看看参考代码对比一下思路。",
]


def agent_tutor_node(state: SessionState) -> Command:
    """Route after agent judging based on the verdict.

    Args:
        state: Session state with ``last_verdict``, ``warm_feedback``,
               ``repair_suggestion``, ``judge_cycle`` set by
               ``agent_judge_node``.

    Returns:
        Command routing to ``planner_node`` (AC) or
        ``wait_for_submit_node`` (WA/RE/TLE).
    """
    verdict = state.last_verdict or ""
    cycle = state.judge_cycle
    logger.info("▶ agent_tutor_node() — verdict=%s cycle=%d", verdict, cycle)

    # ── AC: 全部通过 → 完成（设 phase=reviewing，前端根据此状态显示"下一题"按钮）──
    if verdict == "AC":
        logger.info("AC on cycle %d — session done, waiting for user to request next problem", cycle)
        return Command(
            update={
                "status": "done",
                "phase": "reviewing",
            },
            goto="__end__",
        )

    # ── 未通过 → 循环等待用户修改 ──
    logger.info("Not AC (verdict=%s) — routing back to wait_for_submit (cycle %d)", verdict, cycle)
    return Command(
        update={
            "status": "awaiting_submit",
        },
        goto="wait_for_submit_node",
    )