"""回归测试：agent 出题对话的平滑衔接（修复 对话衔接-1/2/3）。

- is_ready 时收尾回复固定为「正在生成题目」提示，不再让自由模型吐出
  「题目信息遗漏」这类错位文案（修复-1）；回复与路由判定来自同一次
  analyze_user_intent 调用（修复-2）。
- is_ready 时后端立即置 status="awaiting_problem"，让前端进入「生成中」
  视图（修复-3 后端侧）。
"""

import asyncio
from fastapi import BackgroundTasks
from unittest.mock import AsyncMock, MagicMock, patch

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
    """拼接 SSE 流里的真实文本。

    每个事件是 `data: {"t": "..."}` 的 JSON 包裹（见 chat._sse_payload），
    需解析内层 "t" 再拼接，否则中文被 _chunk_text 切块后子串断言会假失败。
    """
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
async def test_agent_dialog_ready_uses_fixed_message_and_awaiting_problem():
    record: list = []
    fake_graph = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake_graph), \
         patch(
            _ANALYZE,
            return_value=DialogIntent(is_ready=True, topic="动态规划", difficulty="medium"),
        ):
        resp = await chat_router.chat_with_tutor_stream("sid-1", {"message": "准备好了"}, background_tasks=BackgroundTasks())
        text = await _collect(resp)
    await asyncio.sleep(0)  # 让后台 _safe_invoke 任务有机会调度

    # 收尾回复应为固定 ready 文案，而非自由模型吐出的「题目信息遗漏」
    assert "题目信息遗漏" not in text
    assert ("正在为你准备" in text) or ("请稍等" in text)

    # 后端应立即置 awaiting_problem，让前端进入生成中视图
    assert any(
        v.get("agent_dialog_complete") is True and v.get("status") == "awaiting_problem"
        for v in record
    )


@pytest.mark.asyncio
async def test_agent_dialog_not_ready_uses_intent_next_message():
    record: list = []
    fake_graph = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake_graph), \
         patch(
            _ANALYZE,
            return_value=DialogIntent(is_ready=False, next_message="那难度想从哪个开始？"),
         ):
        resp = await chat_router.chat_with_tutor_stream("sid-2", {"message": "随便"}, background_tasks=BackgroundTasks())
        text = await _collect(resp)
    await asyncio.sleep(0)

    # 非 ready 的回复来自同一次判定的 next_message（合并，无第二个模型）
    assert "那难度想从哪个开始？" in text
    # 非 ready 不应触发出题
    assert all(v.get("agent_dialog_complete") is not True for v in record)
