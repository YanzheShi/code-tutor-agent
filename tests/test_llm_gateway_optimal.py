"""Unit tests for LlmGateway.generate_optimal retry behavior.

generate_optimal 现在带重试：空响应 / 异常都应触发重试，耗尽 max_retries 才返回 None。
（重试内的退避 sleep 在单测里被 patch 掉，避免拖慢。）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import code_tutor_agent.config as config_mod  # noqa: E402
from code_tutor_agent.generation.gateways import llm as llm_gateway_mod  # noqa: E402
from code_tutor_agent.generation.gateways.llm import LlmGateway  # noqa: E402


def _fake_response(text: str) -> MagicMock:
    m = MagicMock()
    m.content = text
    return m


def _patch_llm(monkeypatch, fake_llm) -> None:
    # generate_optimal 内部 `from code_tutor_agent.config import get_llm`，
    # 每次调用都重新解析该属性，故 patch config.get_llm 即可生效。
    monkeypatch.setattr(config_mod, "get_llm", lambda *a, **k: fake_llm)
    monkeypatch.setattr(llm_gateway_mod.time, "sleep", lambda *a, **k: None)


def test_generate_optimal_retries_on_empty_then_succeeds(monkeypatch):
    """前两次返回空响应，第三次拿到有效代码应返回该代码。"""
    calls = {"n": 0}

    def fake_invoke(prompt):
        calls["n"] += 1
        if calls["n"] < 3:
            return _fake_response("")  # 空响应
        return _fake_response(
            "```python\nclass Solution:\n    def twoSum(self, nums, target):\n        return [0, 1]\n```"
        )

    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = fake_invoke
    _patch_llm(monkeypatch, fake_llm)

    gw = LlmGateway()
    code = gw.generate_optimal("t", "d", "easy", max_retries=3)

    assert code is not None
    assert calls["n"] == 3


def test_generate_optimal_returns_none_after_exhausting_retries(monkeypatch):
    """连续空响应耗尽重试次数后应返回 None，且调用次数等于 max_retries。"""
    fake_llm = MagicMock()
    fake_llm.invoke.return_value = _fake_response("")  # 始终空响应
    _patch_llm(monkeypatch, fake_llm)

    gw = LlmGateway()
    code = gw.generate_optimal("t", "d", "easy", max_retries=3)

    assert code is None
    assert fake_llm.invoke.call_count == 3


def test_generate_optimal_retries_on_exception(monkeypatch):
    """首次抛异常也应重试，第二次成功返回代码。"""
    calls = {"n": 0}

    def fake_invoke(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient network error")
        return _fake_response("```python\nclass Solution:\n    def f(self):\n        pass\n```")

    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = fake_invoke
    _patch_llm(monkeypatch, fake_llm)

    gw = LlmGateway()
    code = gw.generate_optimal("t", "d", "easy", max_retries=3)

    assert code is not None
    assert calls["n"] == 2
