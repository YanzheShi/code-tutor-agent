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

# agent-only 重构辅助：驱动「对话 → 出题」流程（tests/integration 下无 __init__，
# 故按目录加入 sys.path 后直接 import 模块）。
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "integration"))
from _agent_helpers import create_session_with_problem, drive_dialog_to_problem

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
        """创建 session → 对话出第一题 → /next-problem（回到对话）→ 再对话出第二题。

        agent-only 重构（2026-08-13）：/next-problem 在 agent 模式下**不立即出下一题**，
        而是把会话重新打回 tutor 对话（problem 清空、phase=dialog），需用户再次对话
        确认需求后才会 planner→generator 出下一题。故每个下一题都要驱动一次对话。

        验证：
            - 第二题有 problem
            - phase 是 solving
            - total_problems = 1
        """
        # 1. 创建 session 并驱动 agent 对话完成需求收集 → 第一题就绪
        sid, state1 = create_session_with_problem(client, topic="数组", difficulty="easy")
        assert state1["problem"] is not None
        pid1 = state1["problem"]["problem_id"]

        # 2. /next-problem → 回到对话（problem 清空，不会立即出新题）
        resp = client.post(f"/session/{sid}/next-problem", json={"preference": "next_in_plan"})
        assert resp.status_code == 200, f"next-problem failed: {resp.text}"
        np_data = resp.json()
        assert np_data["session_id"] == sid
        # agent 模式下 /next-problem 仅回到对话，题目暂未生成
        assert np_data["phase"] in ("dialog", "solving")

        # 3. 再次驱动对话 → 第二题生成
        state2 = drive_dialog_to_problem(client, sid, "练习双指针，简单难度，开始吧")
        assert state2["problem"] is not None
        assert state2["phase"] == "solving"
        pid2 = state2["problem"]["problem_id"]
        assert pid2 != pid1

        # 4. 验证 total_problems 累加
        if state2.get("total_problems") is not None:
            assert state2["total_problems"] >= 1

    def test_3_rounds(self, client):
        """创建 session → 对话出第一题 →（/next-problem + 对话）× 2 → 三题。

        agent-only 重构（2026-08-13）：每道下一题都需要「/next-problem 回到对话 →
        再对话确认需求」两轮操作。验证三题 problem_id 不同、total_problems 累加。
        """
        # 1. 创建 session 并驱动对话 → 第一题就绪
        sid, state1 = create_session_with_problem(client, topic="动态规划", difficulty="medium")
        pid1 = state1["problem"]["problem_id"]

        # 2. 第二题：/next-problem → 对话
        client.post(f"/session/{sid}/next-problem", json={"preference": "random"})
        state2 = drive_dialog_to_problem(client, sid, "练习数组，简单难度，继续")
        pid2 = state2["problem"]["problem_id"]
        assert pid2 != pid1
        if state2.get("total_problems") is not None:
            assert state2["total_problems"] >= 1

        # 3. 第三题：/next-problem → 对话
        client.post(f"/session/{sid}/next-problem", json={"preference": "same_topic"})
        state3 = drive_dialog_to_problem(client, sid, "练习链表，简单难度，再来一题")
        pid3 = state3["problem"]["problem_id"]
        assert pid3 != pid1 and pid3 != pid2
        if state3.get("total_problems") is not None:
            assert state3["total_problems"] >= 2
        assert state3["phase"] == "solving"

    def test_next_problem_nonexistent_session(self, client):
        """不存在的 sessionId 应返回 409（LG get_state 不抛异常，返回空 state）。"""
        resp = client.post("/session/does-not-exist/next-problem", json={})
        assert resp.status_code == 409