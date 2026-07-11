"""集成测试：聊天交互。

覆盖场景：
- 创建 session 后向导师提问
- 聊天历史在 state 中可查
- 连续发送多条消息
- 流式聊天
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestChatFlow:
    """聊天交互集成测试。"""

    def test_chat_after_session_creation(self, client):
        """创建 session 后可以发送消息。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        sid = resp.json()["session_id"]
        resp = client.post(f"/session/{sid}/chat", json={"message": "这道题怎么做？"})
        assert resp.status_code == 200

    def test_chat_history_in_state(self, client):
        """聊天后 state 中应有 tutor_messages。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        sid = resp.json()["session_id"]
        client.post(f"/session/{sid}/chat", json={"message": "提示我吧"})
        resp = client.get(f"/session/{sid}/state")
        assert resp.status_code == 200
        assert "tutor_messages" in resp.json()

    def test_multiple_chat_messages(self, client):
        """连续发送多条消息不崩溃。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        sid = resp.json()["session_id"]
        for msg in ["你好", "帮我看看", "还有解法吗？", "谢谢"]:
            resp = client.post(f"/session/{sid}/chat", json={"message": msg})
            assert resp.status_code == 200

    def test_stream_chat_returns_200(self, client):
        """流式聊天返回 200。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        sid = resp.json()["session_id"]
        resp = client.post(f"/session/{sid}/chat/stream", json={"message": "给我一个提示"})
        assert resp.status_code == 200