"""Agent 模式集成测试共享辅助：驱动「对话 → 出题」流程。

agent-only 重构（2026-08-13）后，POST /session 进入 agent_dialog 阶段
（status=dialog），不会立即生成题目；必须由客户端经 ``/chat/stream`` 完成需求对话
（LLM 判定 ``is_ready``）后才会 planner→generator 生成题目。本模块提供统一辅助，
供依赖「题目就绪」的集成测试复用，避免每个文件重复实现轮询逻辑。
"""
from __future__ import annotations

import time

POLL_INTERVAL = 2.0
PROBLEM_TIMEOUT = 300.0  # 出题含 LLM 边界用例生成，可能较慢（实测单次 ~3min）


def drive_dialog_to_problem(client, sid: str, message: str, timeout: float = PROBLEM_TIMEOUT) -> dict:
    """发一条对话消息完成需求收集，轮询直到题目就绪（awaiting_submit）。

    Args:
        client: FastAPI TestClient。
        sid: 会话 id（POST /session 后处于 dialog 阶段）。
        message: 用于完成对话的需求描述（需让 LLM 判定 is_ready）。
        timeout: 最长等待秒数。

    Returns:
        题目就绪时的 state dict。
    """
    resp = client.post(f"/session/{sid}/chat/stream", json={"message": message})
    # 消费 SSE 流以触发后台出题（BackgroundTasks 在响应返回后执行）
    try:
        for _ in resp.iter_text():
            pass
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/session/{sid}/state").json()
        if data.get("problem") and data.get("status") == "awaiting_submit":
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Session {sid} did not generate a problem within {timeout}s")


def create_session_with_problem(
    client,
    topic: str = "数组",
    difficulty: str = "easy",
    message: str = "我想练习数组，简单难度，直接开始吧",
    timeout: float = PROBLEM_TIMEOUT,
) -> tuple[str, dict]:
    """创建 session 并驱动对话直到题目就绪，返回 (sid, state)。"""
    resp = client.post("/session", json={"topic": topic, "difficulty": difficulty})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    state = drive_dialog_to_problem(client, sid, message, timeout=timeout)
    return sid, state
