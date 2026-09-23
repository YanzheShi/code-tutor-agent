"""方案 A 回归：超时兜底后孤儿线程不得污染真实会话 checkpoint。

不依赖 DB：用内存版 FakeGraph 模拟 checkpointer，mock ``invoke_graph_tracked``
模拟「孤儿线程比 fallback 晚落库」，断言真实会话只保留降级题。

根因：``asyncio.to_thread`` 跑的 OS 线程无法被 ``wait_for`` 取消；旧逻辑里该线程
最终把 LLM 题 ``update_state`` 进**同一个** thread_id，覆盖降级链注入的题目。
方案 A 用隔离的 scratch 命名空间，孤儿只写 scratch，成功后再镜像回真实 config。
"""
import asyncio
import time

import pytest

from code_tutor_agent.api.services import generation as gen_mod
from code_tutor_agent.schemas.state import SessionState


class FakeGraph:
    """内存 checkpointer 模拟：checkpoints[thread_id] = state dict。"""

    def __init__(self):
        self.checkpoints: dict = {}
        self.checkpointer = self  # 让 _delete_scratch 能调到 delete_thread

    def update_state(self, config, values, as_node=None):
        tid = config["configurable"]["thread_id"]
        cp = self.checkpoints.setdefault(tid, {})
        cp.update(values)

    def get_state(self, config):
        tid = config["configurable"]["thread_id"]
        values = self.checkpoints.get(tid, {})

        class _S:
            def __init__(self, v):
                self.values = v

        return _S(values)

    def delete_thread(self, tid):
        self.checkpoints.pop(tid, None)


async def _async_noop(*a, **k):
    return None


def _make_fast_invoke(captured):
    def _invoke(graph, graph_input, config, entry=""):
        captured["scratch_config"] = config
        tid = config["configurable"]["thread_id"]
        graph.checkpoints[tid] = dict(graph_input)
        graph.checkpoints[tid]["problem"] = {"problem_id": 7, "title": "OK problem"}
        return graph.checkpoints[tid]

    return _invoke


def _make_slow_invoke(captured):
    def _invoke(graph, graph_input, config, entry=""):
        captured["scratch_config"] = config
        tid = config["configurable"]["thread_id"]
        # 故意比 wait_for 超时更晚完成：模拟「orphan 线程」晚于 fallback 落库
        time.sleep(0.3)
        graph.checkpoints[tid] = dict(graph_input)
        graph.checkpoints[tid]["problem"] = {"problem_id": 999, "title": "LLM problem"}
        return graph.checkpoints[tid]

    return _invoke


# ── run_chat_generation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_generation_success_mirrors_to_real_config(monkeypatch):
    fake = FakeGraph()
    real_config = {"configurable": {"thread_id": "sess1"}}
    fake.checkpoints["sess1"] = {"problem": None, "status": "dialog"}
    captured: dict = {}

    monkeypatch.setattr(gen_mod, "get_graph", lambda: fake)
    monkeypatch.setattr(gen_mod, "invoke_graph_tracked", _make_fast_invoke(captured))
    monkeypatch.setattr(gen_mod, "_schedule_suite", _async_noop)
    monkeypatch.setattr(gen_mod, "CHAT_GENERATION_TIMEOUT", 1.0)

    await gen_mod.run_chat_generation(fake, real_config, "sess1")

    # 成功：真实会话拿到生成的题（镜像回真实 config）
    assert fake.checkpoints["sess1"]["problem"]["problem_id"] == 7
    # scratch 命名空间已被清理
    scratch_id = captured["scratch_config"]["configurable"]["thread_id"]
    assert scratch_id.startswith("sess1:scratch:")
    assert scratch_id not in fake.checkpoints


@pytest.mark.asyncio
async def test_chat_generation_timeout_orphan_does_not_clobber_fallback(monkeypatch):
    fake = FakeGraph()
    real_config = {"configurable": {"thread_id": "sess1"}}
    fake.checkpoints["sess1"] = {"problem": None, "status": "dialog"}
    captured: dict = {}

    monkeypatch.setattr(gen_mod, "get_graph", lambda: fake)
    monkeypatch.setattr(gen_mod, "invoke_graph_tracked", _make_slow_invoke(captured))
    monkeypatch.setattr(gen_mod, "CHAT_GENERATION_TIMEOUT", 0.05)

    fallback_written = {}

    async def fake_fallback(sid, config, values):
        # 降级链把题注入【真实】config
        fake.update_state(
            config,
            {"problem": {"problem_id": 1, "title": "FALLBACK"}, "status": "awaiting_submit"},
            as_node="generator_node",
        )
        fallback_written["ok"] = True

    monkeypatch.setattr(gen_mod, "_fallback_problem", fake_fallback)
    monkeypatch.setattr(gen_mod, "_schedule_suite", _async_noop)

    await gen_mod.run_chat_generation(fake, real_config, "sess1")

    # 等孤儿线程落库
    await asyncio.sleep(0.4)

    assert fallback_written.get("ok") is True
    # 真实会话只保留降级题，未被孤儿（LLM problem）覆盖
    assert fake.checkpoints["sess1"]["problem"]["title"] == "FALLBACK"
    # 孤儿确实写到了隔离的 scratch 命名空间（证明隔离生效）
    scratch_id = captured["scratch_config"]["configurable"]["thread_id"]
    assert scratch_id.startswith("sess1:scratch:")
    assert fake.checkpoints[scratch_id]["problem"]["title"] == "LLM problem"


# ── run_generation（create-session 快路径，同款隔离） ───────────────────────────


@pytest.mark.asyncio
async def test_run_generation_success_mirrors_to_real_config(monkeypatch):
    fake = FakeGraph()
    captured: dict = {}

    monkeypatch.setattr(gen_mod, "get_graph", lambda: fake)
    monkeypatch.setattr(gen_mod, "build_run_config", lambda *a, **k: {"configurable": {"thread_id": "sess2"}})
    monkeypatch.setattr(gen_mod, "invoke_graph_tracked", _make_fast_invoke(captured))
    monkeypatch.setattr(gen_mod, "_schedule_suite", _async_noop)
    monkeypatch.setattr(gen_mod, "GENERATION_TIMEOUT", 1.0)

    initial_dict = SessionState(session_id="sess2", user_id="u2").model_dump()
    await gen_mod.run_generation("sess2", initial_dict)

    assert fake.checkpoints["sess2"]["problem"]["problem_id"] == 7
    scratch_id = captured["scratch_config"]["configurable"]["thread_id"]
    assert scratch_id.startswith("sess2:scratch:")
    assert scratch_id not in fake.checkpoints


@pytest.mark.asyncio
async def test_run_generation_timeout_orphan_does_not_clobber_fallback(monkeypatch):
    fake = FakeGraph()
    captured: dict = {}

    monkeypatch.setattr(gen_mod, "get_graph", lambda: fake)
    monkeypatch.setattr(gen_mod, "build_run_config", lambda *a, **k: {"configurable": {"thread_id": "sess2"}})
    monkeypatch.setattr(gen_mod, "invoke_graph_tracked", _make_slow_invoke(captured))
    monkeypatch.setattr(gen_mod, "GENERATION_TIMEOUT", 0.05)

    fallback_written = {}

    async def fake_fallback(sid, config, values):
        fake.update_state(
            config,
            {"problem": {"problem_id": 1, "title": "FALLBACK"}, "status": "awaiting_submit"},
            as_node="generator_node",
        )
        fallback_written["ok"] = True

    monkeypatch.setattr(gen_mod, "_fallback_problem", fake_fallback)
    monkeypatch.setattr(gen_mod, "_schedule_suite", _async_noop)

    initial_dict = SessionState(session_id="sess2", user_id="u2").model_dump()
    await gen_mod.run_generation("sess2", initial_dict)

    await asyncio.sleep(0.4)

    assert fallback_written.get("ok") is True
    assert fake.checkpoints["sess2"]["problem"]["title"] == "FALLBACK"
    scratch_id = captured["scratch_config"]["configurable"]["thread_id"]
    assert scratch_id.startswith("sess2:scratch:")
    assert fake.checkpoints[scratch_id]["problem"]["title"] == "LLM problem"
