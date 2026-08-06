"""回归测试：AC 后「继续出题」只重入出题对话，不直接生成下一道题。

2026-07-22 需求：practice 模式下 AC 后点「继续出题」，应回到出题对话给提示，
而不是直接出下一题（critic→planner→generator）。后端 next-problem 收到
preference='continue_dialog' 时，无论 practice/agent 都走 agent 重入对话分支，
切到 agent 模式、status/phase=dialog、problem=None。
"""
import asyncio
import types
from unittest.mock import patch

from fastapi import HTTPException

import code_tutor_agent.api.routers.session as session_router
from code_tutor_agent.api.routers.session import NextProblemReq


class _FakeGraph:
    def __init__(self, values: dict):
        self._values = dict(values)
        self.updates = []

    def get_state(self, config):
        return types.SimpleNamespace(
            values=dict(self._values),
            next=["wait_for_submit_node"],
        )

    def update_state(self, config, values, as_node=None):
        self._values.update(values)
        self.updates.append((values, as_node))

    def invoke(self, *args, **kwargs):
        if args and args[0] is not None:
            self.updates.append(({"mode": "practice", "phase": "generating"}, "critic_node"))


def _practice_ac_state() -> dict:
    return {
        "mode": "practice",
        "phase": "reviewing",
        "last_verdict": "AC",
        "judge_report": {"verdict": "AC"},
        "problem_history": [],
        "tutor_messages": [],
        "agent_dialog_history": [],
    }


def test_continue_dialog_reenters_dialog_without_generating():
    graph = _FakeGraph(_practice_ac_state())

    with patch.object(session_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.db.database.touch_session", return_value=None):
        result = asyncio.run(
            session_router.next_problem("s1", NextProblemReq(preference="continue_dialog"))
        )

    # 不直接出下一题：problem 为空，phase 回到 dialog
    assert result.problem is None
    assert result.phase == "dialog"

    # 重入对话时把模式切到 agent，并重置 status/phase
    last_update, _as_node = graph.updates[-1]
    assert last_update.get("mode") == "agent"
    assert last_update.get("status") == "dialog"
    assert last_update.get("phase") == "dialog"


def test_normal_next_in_plan_still_generates_for_practice():
    # 反向校验：practice 模式用 next_in_plan（非 AC 续题）不应走重入对话分支
    graph = _FakeGraph(_practice_ac_state())

    with patch.object(session_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.db.database.touch_session", return_value=None):
        try:
            asyncio.run(
                session_router.next_problem("s1", NextProblemReq(preference="next_in_plan"))
            )
        except HTTPException:
            # FakeGraph 不会真出题，normal 分支会因 problem 为空抛 500，符合预期
            pass

    # practice + next_in_plan 没有进入重入对话分支（未把模式切成 agent）
    assert graph.updates, "normal 分支应有一次 critic_node 写入"
    assert all(u[0].get("mode") != "agent" for u in graph.updates)
