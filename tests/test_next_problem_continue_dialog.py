"""回归测试：AC 后「继续出题」只重入出题对话，不直接生成下一道题。

2026-07-22 需求：AC 后点「继续出题」应回到出题对话给提示，而不是直接出下一题。
2026-09-23 清理：normal 模式（practice/interview/debug_theatre）已删除，
next-problem 只剩「重入导师对话」这一条路径 —— 无论 frontend 传什么 preference、
历史 checkpoint 里是什么 mode，都切到 agent、status/phase=dialog、problem=None。
"""
import asyncio
import types
from unittest.mock import patch

import code_tutor_agent.api.routers.session as session_router
from code_tutor_agent.api.routers.session import NextProblemReq

# 直调端点时手动提供登录态（多用户改造后端点带 current 依赖）
_FAKE_USER = {"id": "1", "email": "t@test.com", "role": "user"}


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
        # 端点会显式判空 values / session_id（与 run.py 同款判据，2026-09-23 起），
        # 真实会话的 state 必然有此字段 —— fake state 也必须给，否则会被判 404。
        "session_id": "s1",
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

    # patch owner 校验：开发库残留 s1 归属 'default'（多用户改造前存量行），
    # 不 patch 会 404（与第六轮 handoff/autogen/reentry_guard 同类修法）
    with patch.object(session_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.db.database.touch_session", return_value=None), \
         patch.object(session_router, "get_session_owner", return_value=_FAKE_USER["id"]):
        result = asyncio.run(
            session_router.next_problem("s1", NextProblemReq(preference="continue_dialog"), current=_FAKE_USER)
        )

    # 不直接出下一题：problem 为空，phase 回到 dialog
    assert result.problem is None
    assert result.phase == "dialog"

    # 重入对话时把模式切到 agent，并重置 status/phase
    last_update, _as_node = graph.updates[-1]
    assert last_update.get("mode") == "agent"
    assert last_update.get("status") == "dialog"
    assert last_update.get("phase") == "dialog"


def test_next_in_plan_also_reenters_dialog():
    """normal 模式已删除（2026-09-23）：任何 preference 都重入导师对话。

    旧契约（practice + next_in_plan → critic→planner→generator 直接出下一题）随
    normal 模式一并删除。这里用老 mode=practice 的 checkpoint 反向固定新行为：
    不再有任何 critic_node 写入，一律切 agent + phase=dialog。
    """
    graph = _FakeGraph(_practice_ac_state())

    with patch.object(session_router, "get_graph", return_value=graph), \
         patch("code_tutor_agent.db.database.touch_session", return_value=None), \
         patch.object(session_router, "get_session_owner", return_value=_FAKE_USER["id"]):
        result = asyncio.run(
            session_router.next_problem("s1", NextProblemReq(preference="next_in_plan"), current=_FAKE_USER)
        )

    assert result.problem is None
    assert result.phase == "dialog"
    last_update, _as_node = graph.updates[-1]
    assert last_update.get("mode") == "agent"
    assert last_update.get("status") == "dialog"
    # 关键：不再走 normal 分支（无 critic_node 写入）
    assert all(as_node != "critic_node" for _, as_node in graph.updates)
