"""回归测试：agent 模式 is_ready 后，题目生成由 BackgroundTasks 可靠触发。

核心验证点：旧实现用 `asyncio.create_task`（定义在 SSE 流式响应内部，
连接关闭后极易被取消）触发 graph.invoke，导致 problem 永不写入、
前端卡在「马上就好」需手动输入才跳。改用 FastAPI `BackgroundTasks` 后，
即便 TestClient 在响应返回后才执行后台任务，graph.invoke 仍被调用、
problem 与 status="awaiting_submit" 被写入，前端轮询即可自动跳转。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from unittest.mock import MagicMock, patch

from code_tutor_agent.agents.agent_dialog import DialogIntent
from code_tutor_agent.api.routers import chat as chat_router

# analyze_user_intent 在路由函数体内按名导入，需 patch 其源模块属性
_ANALYZE = "code_tutor_agent.agents.agent_dialog.analyze_user_intent"


def _make_fake_graph(record):
    graph = MagicMock()
    base = {
        "status": "dialog", "mode": "agent", "agent_dialog_complete": False,
        "agent_dialog_history": [], "tutor_messages": [],
        "topic": "", "difficulty": "", "problem": None,
    }

    def _get_state(config):
        snap = MagicMock()
        snap.values = dict(base)
        snap.next = ()
        return snap

    def _update(config, values, as_node=None):
        record.append(values)
        base.update(values)

    def _invoke(inp, config):
        # 模拟 planner → generator：写入题目并置 awaiting_submit
        base["problem"] = {"problem_id": 1, "title": "滑动窗口", "starter_code": "pass"}
        base["status"] = "awaiting_submit"
        return None

    graph.get_state.side_effect = _get_state
    graph.update_state.side_effect = _update
    graph.invoke.side_effect = _invoke
    return graph


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(chat_router.router)
    return TestClient(app)


def test_background_invoke_writes_problem_after_response(client):
    record = []
    fake = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake), \
         patch(_ANALYZE, return_value=DialogIntent(is_ready=True, topic="滑动窗口", difficulty="medium")):
        resp = client.post("/sid-1/chat/stream", json={"message": "出一个滑动窗口题吧"})

    assert resp.status_code == 200
    text = resp.text
    # 收尾文案正确，且无错位「题目信息遗漏」
    assert "题目信息遗漏" not in text
    assert ("请稍等" in text) or ("马上就好" in text)

    # 关键：BackgroundTasks 在响应结束后仍执行了 graph.invoke
    # （旧 asyncio.create_task 方案会因连接关闭被取消而漏跑，problem 永不写入）
    assert fake.invoke.called, "BackgroundTasks 未触发 graph.invoke —— problem 不会被写入"

    # 模拟最终状态：problem 已写入、status 已切到 awaiting_submit
    final = fake.get_state({"configurable": {"thread_id": "sid-1"}})
    assert final.values["problem"] is not None
    assert final.values["status"] == "awaiting_submit"


def test_ready_sets_awaiting_problem_immediately(client):
    record = []
    fake = _make_fake_graph(record)
    with patch.object(chat_router, "get_graph", return_value=fake), \
         patch(_ANALYZE, return_value=DialogIntent(is_ready=True, topic="滑动窗口", difficulty="medium")):
        client.post("/sid-1/chat/stream", json={"message": "出一个滑动窗口题吧"})

    # 后端在流式阶段立即置 awaiting_problem，前端可立刻进入「生成中」视图
    assert any(
        v.get("agent_dialog_complete") is True and v.get("status") == "awaiting_problem"
        for v in record
    )
