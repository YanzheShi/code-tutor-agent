"""llm_failover 单测：主 key 撞限流 → 换 ALT 重发；其它错误原样上抛。"""
from __future__ import annotations

import pytest

from code_tutor_agent.llm_failover import (
    failover_enabled,
    invoke_with_failover,
    is_rate_limit_error,
    output_token_cap_from_error,
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


# ── 输出配额 400：不换 key，按网关上限重发一次 ──────────────

_CAP_400 = Exception(
    "Error code: 400 - {'error': {'message': 'max_tokens exceeds the limit of 4096 "
    "(request id: x)', 'type': 'AgnesAI_error', 'code': 'invalid_request'}}"
)


def _capped_primary(behavior):
    llm = _FakeLLM("primary", [behavior])
    llm.openai_api_base = "https://apihub.agnes-ai.com/v1"
    llm.metadata = {"model_name": "agnes-2.5-flash"}
    return llm


def test_output_token_cap_parsing():
    assert output_token_cap_from_error(_CAP_400) == 4096
    assert output_token_cap_from_error(Exception("429 too many requests")) is None


def test_output_cap_retries_at_gateway_limit(monkeypatch):
    """400 上限错误 → 学到 4096 → 用同用途重建模型重发一次，不切 ALT key。"""
    from code_tutor_agent import config as cfg

    monkeypatch.setattr(cfg, "_LEARNED_OUTPUT_TOKEN_LIMITS", {})
    fake_capped = _FakeLLM("capped", [("ok", "from-capped")])
    calls = {}

    def fake_get_llm(purpose, alias=None, **kwargs):
        calls["purpose"] = purpose
        calls["alias"] = alias
        calls["kwargs"] = kwargs
        return fake_capped

    monkeypatch.setattr("code_tutor_agent.llm_failover.get_llm", fake_get_llm)

    result = invoke_with_failover(
        _capped_primary(_CAP_400), lambda m: m.invoke("hi"), purpose="problem"
    )
    assert result == "from-capped"
    assert calls == {"purpose": "problem", "alias": None, "kwargs": {"max_tokens": 4096}}
    assert cfg.output_token_limit(
        "https://apihub.agnes-ai.com/v1", "agnes-2.5-flash"
    ) == 4096


def test_output_cap_without_purpose_reraises(monkeypatch):
    """不知道用途就无法重建模型，只能原样上抛（但仍记下上限供后续调用）。"""
    from code_tutor_agent import config as cfg

    monkeypatch.setattr(cfg, "_LEARNED_OUTPUT_TOKEN_LIMITS", {})
    fake_primary = _capped_primary(_CAP_400)
    with pytest.raises(Exception, match="max_tokens exceeds"):
        invoke_with_failover(fake_primary, lambda m: m.invoke("hi"), purpose="")
    assert fake_primary.calls == 1
    assert cfg.output_token_limit(
        "https://apihub.agnes-ai.com/v1", "agnes-2.5-flash"
    ) == 4096


def test_output_cap_retry_failure_reraised(monkeypatch):
    from code_tutor_agent import config as cfg

    monkeypatch.setattr(cfg, "_LEARNED_OUTPUT_TOKEN_LIMITS", {})
    fake_capped = _FakeLLM("capped", [ValueError("still broken")])

    def fake_get_llm(purpose, alias=None, **kwargs):
        return fake_capped

    monkeypatch.setattr("code_tutor_agent.llm_failover.get_llm", fake_get_llm)

    with pytest.raises(ValueError, match="still broken"):
        invoke_with_failover(
            _capped_primary(_CAP_400), lambda m: m.invoke("hi"), purpose="problem"
        )
    assert fake_capped.calls == 1
