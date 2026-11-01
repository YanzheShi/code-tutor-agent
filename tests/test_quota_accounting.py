"""做题配额账本回归（2026-11-01 双计事故）。

锁死两个业务不变量：
1. 建会话只开对话，不扣做题配额（见 tests/test_trial_user_flow.py 的路由级用例）；
2. chat/stream 的 is_ready 出题分支是唯一扣点：一次出题恰好扣 1，N 次扣 N。

测试不碰真实 LLM / DB：
- chat 路由的 get_graph / get_session_owner / 后台出题任务全部替换为假实现；
- analyze_user_intent 在路由函数体内按名导入，patch 其源模块属性。
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from code_tutor_agent.agents.agent_dialog import DialogIntent
from code_tutor_agent.api import quota as quota_mod
from code_tutor_agent.api.auth import get_current_user
from code_tutor_agent.api.routers import chat as chat_router

_ANALYZE = "code_tutor_agent.agents.agent_dialog.analyze_user_intent"


class _FakeGraph:
    """最小 LangGraph 替身：只需要 get_state / update_state。"""

    def __init__(self) -> None:
        self.base = {
            "status": "dialog",
            "mode": "agent",
            "agent_dialog_complete": False,
            "agent_dialog_history": [],
            "tutor_messages": [],
            "topic": "",
            "difficulty": "",
            "problem": None,
        }

    def get_state(self, config):
        snap = type("Snap", (), {})()
        snap.values = dict(self.base)
        snap.next = ()
        return snap

    def update_state(self, config, values, as_node=None):
        self.base.update(values)


async def _fake_analyze(*args, **kwargs) -> DialogIntent:
    return DialogIntent(
        is_ready=True,
        topic="数组",
        difficulty="easy",
        next_message="好的，我来为你准备题目。",
    )


async def _noop_background(*args, **kwargs) -> None:
    return None


def _make_client(uid: str = "1", role: str = "user"):
    app = FastAPI()
    app.include_router(chat_router.router)
    holder = {"current": {"id": uid, "email": f"u{uid}@test.com", "role": role}}
    app.dependency_overrides[get_current_user] = lambda: holder["current"]
    return TestClient(app), holder


def _patch_chat(monkeypatch, holder):
    # 每个请求拿一个新的假图；后台出题任务置空，避免真实 LLM
    monkeypatch.setattr(chat_router, "get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(
        chat_router, "get_session_owner", lambda sid: str(holder["current"]["id"])
    )
    monkeypatch.setattr(chat_router, "_run_graph_and_generate_tests", _noop_background)
    monkeypatch.setattr(_ANALYZE, _fake_analyze)


def _problem_used(uid: str) -> int:
    """用户维度 24h 桶内当前有效记录数。"""
    with quota_mod._lock:
        return len(quota_mod._windows.get(f"pu:{uid}:d", ()))


def _ip_used_total() -> int:
    """所有 IP 维度 24h 桶的记录总数（本测试只有一个 TestClient IP）。"""
    with quota_mod._lock:
        return sum(
            len(bucket)
            for key, bucket in quota_mod._windows.items()
            if key.startswith("pi:") and key.endswith(":d")
        )


def _sse_text(resp) -> str:
    """把 SSE 分片拼回完整回复（分片可能把关键词从中间截断）。"""
    parts: list[str] = []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "__DONE__":
            continue
        try:
            parts.append(json.loads(payload).get("t", ""))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


@pytest.fixture(autouse=True)
def _quota_enabled(monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    quota_mod.reset_all()
    yield
    quota_mod.reset_all()


def test_one_ready_turn_charges_exactly_once(monkeypatch):
    """一次出题意图 = 扣 1 次（不是 0，也不是 2）。"""
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "5")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")
    client, holder = _make_client(uid="1", role="user")
    _patch_chat(monkeypatch, holder)

    r = client.post("/sid-1/chat/stream", json={"message": "出一道数组题"})
    assert r.status_code == 200, r.text
    assert _problem_used("1") == 1
    # 确认真的走了出题确认文案，而不是静默降级
    reply = _sse_text(r)
    assert ("请稍等" in reply) or ("马上就好" in reply)


def test_n_ready_turns_charge_n_times_then_downgrade(monkeypatch):
    """N 道题 = N 次扣费；触顶后的出题意图降级为普通回复，不继续扣。"""
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "2")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")
    client, holder = _make_client(uid="1", role="user")
    _patch_chat(monkeypatch, holder)

    for i in range(2):
        r = client.post(f"/sid-{i}/chat/stream", json={"message": "出一道数组题"})
        assert r.status_code == 200, r.text
    assert _problem_used("1") == 2

    r3 = client.post("/sid-3/chat/stream", json={"message": "再出一道数组题"})
    assert r3.status_code == 200, r3.text
    assert "做题额度已用完" in _sse_text(r3)
    assert _problem_used("1") == 2  # 触顶不再累计


def test_trial_ip_anchor_charges_at_chat_not_session(monkeypatch):
    """体验账号的 IP 锚点仍在真实扣点（chat 出题）上生效。

    旧实现把扣点放在建会话，导致 5 次建会话就能耗光额度；现在建会话不扣，
    IP 维度必须在 chat 的 is_ready 分支拦住同一网络的第二个体验账号。
    """
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "0")  # 关用户维度，只测 IP 锚
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "1")
    client, holder = _make_client(uid="1", role="test")
    _patch_chat(monkeypatch, holder)

    r1 = client.post("/sid-1/chat/stream", json={"message": "出一道数组题"})
    assert r1.status_code == 200, r1.text
    assert _ip_used_total() == 1

    # 同 IP 换一个体验账号：出题意图被 IP 锚拦住，文案走注册转化钩子
    holder["current"] = {"id": "2", "email": "u2@test.com", "role": "test"}
    r2 = client.post("/sid-2/chat/stream", json={"message": "出一道数组题"})
    assert r2.status_code == 200, r2.text
    assert "注册" in _sse_text(r2)
    assert _ip_used_total() == 1
