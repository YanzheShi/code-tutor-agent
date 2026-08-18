"""回归测试：next-problem 重入 dialog 卡死的确定性兜底修复。

根因：analyze_user_intent 的 prompt 在「对话初期(1-2 轮)」分支与
「随机 / 出题即 ready」分支存在矛盾，导致 next-problem 切换后用户发
「请随机给我出一道算法题」约 50% 概率被误判 is_ready=False，使连续做题
链路永久卡在 dialog 态（无法触发出题）。

修复：chat._handle_agent_dialog_stream 中，analyze_user_intent 返回
is_ready=False 但用户消息命中显式「要求出题 / 交 AI 决定」关键词时，
确定性地强制 is_ready=True，从而触发出题、解除卡死。

本测试用 patch 模拟 LLM 误判，验证兜底确实生效且不会误伤正常追问。
"""

import asyncio
from fastapi import BackgroundTasks
from unittest.mock import MagicMock, patch

import pytest

from code_tutor_agent.agents.agent_dialog import DialogIntent
from code_tutor_agent.api.routers import chat as chat_router

# analyze_user_intent 在路由函数体内按名导入，需 patch 其源模块属性
_ANALYZE = "code_tutor_agent.agents.agent_dialog.analyze_user_intent"


def _make_fake_graph(record: list):
    graph = MagicMock()
    state = MagicMock()
    state.values = {
        "status": "dialog",
        "mode": "agent",
        "agent_dialog_complete": False,
        "agent_dialog_history": [],
        "tutor_messages": [],
        "topic": "",
        "difficulty": "",
    }
    graph.get_state.return_value = state

    def _update(config, values, as_node=None):
        record.append(values)

    graph.update_state.side_effect = _update
    graph.invoke = MagicMock(return_value=None)
    return graph


async def _collect(resp) -> str:
    """拼接 SSE 流里的真实文本（内层 "t" 字段）。"""
    import json

    chunks = []
    async for part in resp.body_iterator:
        if part.startswith("data: ") and "__DONE__" not in part:
            payload = part[len("data: "):].rstrip("\n")
            try:
                chunks.append(json.loads(payload).get("t", ""))
            except (json.JSONDecodeError, TypeError):
                chunks.append(payload)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_guard_forces_ready_on_explicit_generate_message():
    """模拟 LLM 误判 is_ready=False，但用户消息含显式『出题』关键词 → 兜底强制 ready。"""
    record: list = []
    fake_graph = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake_graph), \
         patch(
            _ANALYZE,
            return_value=DialogIntent(
                is_ready=False,  # 模拟 prompt 矛盾导致的误判（即历史卡死根因）
                next_message="你想练哪个方向？",
            ),
         ):
        resp = await chat_router.chat_with_tutor_stream(
            "sid-guard",
            {"message": "请随机给我出一道算法题，不用确认，直接开始出题。"},
            background_tasks=BackgroundTasks(),
        )
        await _collect(resp)
    await asyncio.sleep(0)

    # 兜底生效：即便 LLM 误判，也应触发出题（status=awaiting_problem）
    assert any(
        v.get("agent_dialog_complete") is True and v.get("status") == "awaiting_problem"
        for v in record
    ), "guard 未强制 is_ready=True：next-problem 重入仍会卡死"


@pytest.mark.asyncio
async def test_guard_forces_ready_on_random_keyword_only():
    """仅含『随便 / 你决定』等交 AI 决定关键词、无『出题』字眼时，兜底同样生效。"""
    record: list = []
    fake_graph = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake_graph), \
         patch(
            _ANALYZE,
            return_value=DialogIntent(is_ready=False, next_message="推荐几个方向？"),
         ):
        resp = await chat_router.chat_with_tutor_stream(
            "sid-guard-rand",
            {"message": "随便，你帮我决定一道题吧"},
            background_tasks=BackgroundTasks(),
        )
        await _collect(resp)
    await asyncio.sleep(0)

    assert any(
        v.get("agent_dialog_complete") is True and v.get("status") == "awaiting_problem"
        for v in record
    ), "含『随便 / 你决定』时应兜底强制 ready"


@pytest.mark.asyncio
async def test_guard_does_not_fire_without_explicit_keyword():
    """无显式『出题 / 随机』关键词时，兜底不应误触发（保留正常追问流程）。"""
    record: list = []
    fake_graph = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake_graph), \
         patch(
            _ANALYZE,
            return_value=DialogIntent(
                is_ready=False,
                next_message="你想练哪个方向？数组、链表还是动态规划？",
            ),
         ):
        resp = await chat_router.chat_with_tutor_stream(
            "sid-no-guard",
            {"message": "我想练算法题"},  # 无显式出题 / 随机关键词
            background_tasks=BackgroundTasks(),
        )
        await _collect(resp)
    await asyncio.sleep(0)

    # 兜底不应误伤：未命中关键词 → 不触发出题
    assert all(v.get("agent_dialog_complete") is not True for v in record)
