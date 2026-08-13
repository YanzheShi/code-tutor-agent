"""Agent 路由节点 — LLM 判题后决定下一跳的 LangGraph 节点。

该节点在 agent_judge_node 给出 WA 时被调用，把 graph 重新暂停在
wait_for_submit_node 等待用户修改后重提交。AC 分支不再经此节点
（由 agent_judge_router 直接路由到 update_profile_node / wait_for_submit_node）。
"""

from __future__ import annotations

import logging

from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)


def agent_tutor_node(state: SessionState) -> dict:
    """Route after a non-AC judge cycle back to wait_for_submit.

    Args:
        state: Session state with ``last_verdict`` set by ``agent_judge_node``.

    Returns:
        Plain state update (no Command); graph routes via the static
        edge ``agent_tutor_node → wait_for_submit_node``.
    """
    verdict = state.last_verdict or ""
    cycle = state.judge_cycle
    logger.info("▶ agent_tutor_node() — verdict=%s cycle=%d", verdict, cycle)

    # 未通过 → 循环等待用户修改（AC 不在此分支）
    return {"status": "awaiting_submit"}
