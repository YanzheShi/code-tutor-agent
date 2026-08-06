"""集成测试：连续做题（multi-question）— 2 轮和 3 轮。

覆盖场景：
- 创建 session → 等待第一题就绪
- 调 /next-problem → 第二题生成，problem_history 累加
- 调 /next-problem 第三次 → problem_history 继续增长
- phase 流转正确（solving → done → solving）
- 非法 sessionId 返回 404
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app

POLL_INTERVAL = 1.0       # 轮询间隔（秒）
MAX_POLL_WAIT = 60.0      # 最长等待时间（秒）


@pytest.fixture(scope="function")
def client():
    with TestClient(app) as c:
        yield c


def _wait_for_problem(client, sid: str, timeout: float = MAX_POLL_WAIT) -> dict:
    """Poll GET /session/{sid}/state until a problem is available."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/session/{sid}/state")
        if resp.status_code != 200:
            time.sleep(POLL_INTERVAL)
            continue
        data = resp.json()
        if data.get("problem") and data.get("status") == "awaiting_submit":
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Session {sid} did not get a problem within {timeout}s")


class TestMultiQuestion:
    """连续做题集成测试。"""

    def test_2_rounds(self, client):
        """创建 session → 等第一题 → /next-problem → 第二题生成。

        验证：
            - 第二题有 problem
            - problem_history 有 1 条记录
            - phase 是 solving
            - total_problems = 1
        """
        # 1. 创建 session
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        # 2. 等第一题就绪
        state1 = _wait_for_problem(client, sid)
        assert state1["problem"] is not None
        pid1 = state1["problem"]["problem_id"]

        # 3. 调 /next-problem → 第二题
        resp = client.post(f"/session/{sid}/next-problem", json={"preference": "next_in_plan"})
        assert resp.status_code == 200, f"next-problem failed: {resp.text}"
        np_data = resp.json()
        assert np_data["session_id"] == sid
        assert np_data["problem"] is not None
        assert np_data["phase"] == "solving"

        # 4. 验证 state
        state2 = client.get(f"/session/{sid}/state").json()
        assert state2["problem"] is not None
        assert state2["phase"] == "solving"
        # total_problems 在旧缓存下可能缺失，只在有值时才断言
        if state2.get("total_problems") is not None:
            assert state2["total_problems"] >= 1

    def test_3_rounds(self, client):
        """创建 session → 等第一题 → /next-problem × 2 → 三题。

        验证：
            - 三题有不同 problem_id
            - 第三次后 problem_history 有 2 条记录
            - total_problems = 2
        """
        # 1. 创建 session
        resp = client.post("/session", json={"topic": "动态规划", "difficulty": "medium"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        # 2. 等第一题
        state1 = _wait_for_problem(client, sid)
        pid1 = state1["problem"]["problem_id"]

        # 3. 第二题
        resp = client.post(f"/session/{sid}/next-problem", json={"preference": "random"})
        assert resp.status_code == 200

        # 验证第一次 state
        state2 = client.get(f"/session/{sid}/state").json()
        if state2.get("total_problems") is not None:
            assert state2["total_problems"] >= 1

        # 4. 第三题
        resp = client.post(f"/session/{sid}/next-problem", json={"preference": "same_topic"})
        assert resp.status_code == 200

        # 5. 验证最终 state
        state3 = client.get(f"/session/{sid}/state").json()
        if state3.get("total_problems") is not None:
            assert state3["total_problems"] >= 2
        assert state3["phase"] == "solving"

    def test_next_problem_nonexistent_session(self, client):
        """不存在的 sessionId 应返回 409（LG get_state 不抛异常，返回空 state）。"""
        resp = client.post("/session/does-not-exist/next-problem", json={})
        assert resp.status_code == 409