"""Agent 模式集成测试共享辅助：驱动「对话 → 出题」流程。

agent-only 重构（2026-08-13）后，POST /session 进入 agent_dialog 阶段
（status=dialog），不会立即生成题目；必须由客户端经 ``/chat/stream`` 完成需求对话
（LLM 判定 ``is_ready``）后才会 planner→generator 生成题目。本模块提供统一辅助，
供依赖「题目就绪」的集成测试复用，避免每个文件重复实现轮询逻辑。

多用户改造（2026-09-06）：所有 /session（含 /chat、/run、/problems）路由统一
Bearer 鉴权。故本模块在发请求前自动登录引导管理员账号，并把
``Authorization: Bearer <token>`` 注入每个请求；token 按 client 实例缓存复用。
登录凭据默认读取环境变量 CTA_TEST_EMAIL / CTA_TEST_PASSWORD，
未配置时回退到引导管理员默认账号（534629255@qq.com / test123456）。
"""
from __future__ import annotations

import os
import time

POLL_INTERVAL = 2.0
PROBLEM_TIMEOUT = 300.0  # 出题含 LLM 边界用例生成，可能较慢（实测单次 ~3min）

# ── 多用户鉴权 ──
_AUTH_EMAIL = os.getenv("CTA_TEST_EMAIL", "534629255@qq.com")
_AUTH_PASSWORD = os.getenv("CTA_TEST_PASSWORD", "test123456")
_token_cache: dict[int, str] = {}


def get_auth_token(client) -> str:
    """登录引导管理员账号，返回 JWT；同一 client 实例内缓存复用。"""
    cache_key = id(client)
    cached = _token_cache.get(cache_key)
    if cached:
        return cached
    resp = client.post(
        "/auth/login",
        json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"
    token = resp.json()["token"]
    _token_cache[cache_key] = token
    return token


def auth_headers(client) -> dict:
    """返回带 Bearer token 的请求头；按需登录并缓存。"""
    return {"Authorization": f"Bearer {get_auth_token(client)}"}


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
    headers = auth_headers(client)
    resp = client.post(f"/session/{sid}/chat/stream", json={"message": message}, headers=headers)
    # 消费 SSE 流以触发后台出题（BackgroundTasks 在响应返回后执行）
    try:
        for _ in resp.iter_text():
            pass
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/session/{sid}/state", headers=headers).json()
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
    headers = auth_headers(client)
    resp = client.post("/session", json={"topic": topic, "difficulty": difficulty}, headers=headers)
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    state = drive_dialog_to_problem(client, sid, message, timeout=timeout)
    return sid, state
