"""llm_failover 单测：主 key 撞限流 → 换 ALT 重发；其它错误原样上抛。"""
from __future__ import annotations

import pytest

from code_tutor_agent.llm_failover import (
    failover_enabled,
    invoke_with_failover,
    is_rate_limit_error,
)


class _FakeLLM:
    """可编程假模型：invoke 时按预设脚本抛错/返回。"""

    def __init__(self, name: str, behaviors: list):
        self.name = name
        self.behaviors = list(behaviors)  # 每次消费一个：Exception 或 ("ok", value)
        self.calls = 0

    def invoke(self, _msgs):
        self.calls += 1
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b[1]  # ("ok", value) → value


def _patch_alt(monkeypatch, model="alt-model", key="alt-key"):
    monkeypatch.setenv("LLM_MODEL_ALT", model)
    monkeypatch.setenv("LLM_API_KEY_ALT", key)
    monkeypatch.delenv("LLM_FAILOVER", raising=False)


def test_is_rate_limit_error_matching():
    assert is_rate_limit_error(Exception("Error code: 429 - Too Many Requests"))
    assert is_rate_limit_error(Exception("Rate limit reached for requests"))
    assert is_rate_limit_error(Exception("quota exceeded for this key"))
    assert not is_rate_limit_error(Exception("connection reset by peer"))
    assert not is_rate_limit_error(ValueError("invalid api key"))


def test_failover_enabled_requires_alt_config(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_ALT", raising=False)
    monkeypatch.delenv("LLM_API_KEY_ALT", raising=False)
    assert not failover_enabled()
    _patch_alt(monkeypatch)
    assert failover_enabled()
    monkeypatch.setenv("LLM_FAILOVER", "0")
    assert not failover_enabled()


def test_rate_limit_switches_to_secondary(monkeypatch):
    _patch_alt(monkeypatch)
    # 主模型：先撞 429；备用模型：直接成功
    fake_secondary = _FakeLLM("secondary", [("ok", "from-alt")])

    called = {}

    def fake_get_llm(purpose, alias=None, **kwargs):
        called["purpose"] = purpose
        called["alias"] = alias
        return fake_secondary

    monkeypatch.setattr("code_tutor_agent.llm_failover.get_llm", fake_get_llm)

    fake_primary = _FakeLLM("primary", [Exception("Error code: 429 - Too Many Requests")])
    result = invoke_with_failover(
        fake_primary, lambda m: m.invoke("hi"), purpose="dialog"
    )
    assert result == "from-alt"
    assert fake_primary.calls == 1 and fake_secondary.calls == 1
    assert called == {"purpose": "dialog", "alias": "secondary"}


def test_non_rate_limit_reraised(monkeypatch):
    _patch_alt(monkeypatch)
    fake_primary = _FakeLLM("primary", [ValueError("invalid request body")])
    with pytest.raises(ValueError):
        invoke_with_failover(fake_primary, lambda m: m.invoke("hi"), purpose="dialog")
    assert fake_primary.calls == 1


def test_no_failover_when_alt_missing(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_ALT", raising=False)
    monkeypatch.delenv("LLM_API_KEY_ALT", raising=False)
    fake_primary = _FakeLLM("primary", [Exception("429 too many requests")])
    with pytest.raises(Exception, match="429"):
        invoke_with_failover(fake_primary, lambda m: m.invoke("hi"), purpose="dialog")
    assert fake_primary.calls == 1  # 未重试


def test_no_failover_for_user_custom_key(monkeypatch):
    """用户自配 key（custom 模式）不降级到平台备用 key。"""
    _patch_alt(monkeypatch)
    from code_tutor_agent import runtime_settings as rs

    token = rs.set_llm_override({"model": "m", "base_url": "u", "api_key": "k"})
    try:
        fake_primary = _FakeLLM("primary", [Exception("429 too many requests")])
        with pytest.raises(Exception, match="429"):
            invoke_with_failover(fake_primary, lambda m: m.invoke("hi"), purpose="dialog")
        assert fake_primary.calls == 1
    finally:
        from code_tutor_agent.runtime_settings import llm_override_ctx

        llm_override_ctx.reset(token)


def test_primary_success_never_touches_secondary(monkeypatch):
    _patch_alt(monkeypatch)
    fake_primary = _FakeLLM("primary", [("ok", "from-primary")])
    result = invoke_with_failover(fake_primary, lambda m: m.invoke("hi"), purpose="dialog")
    assert result == "from-primary"
    assert fake_primary.calls == 1
