"""网关输出配额协商（config.get_llm，2026-11-06 apihub 400 事故）。

防两类回归：
1. 用途诉求超过网关上限 → 请求被 400 拒绝、出题链静默降级；
2. 运行期学到的上限没回填 → 每次先撞一次 400 才降档。
"""
from __future__ import annotations

import pytest

from code_tutor_agent import config as cfg
from code_tutor_agent import runtime_settings


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    """隔离学习缓存/告警去重表，并截获 init_chat_model 的最终参数。"""
    monkeypatch.setattr(cfg, "_LEARNED_OUTPUT_TOKEN_LIMITS", {})
    monkeypatch.setattr(cfg, "_CLAMP_WARNED", set())
    monkeypatch.setattr(runtime_settings, "get_llm_override", lambda: None)
    captured: dict = {}

    def fake_init(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(cfg, "init_chat_model", fake_init)
    yield captured


def _use_gateway(monkeypatch, base_url: str, model: str = "agnes-2.5-flash"):
    monkeypatch.setitem(cfg.LLM_CONFIGS["default"], "base_url", base_url)
    monkeypatch.setitem(cfg.LLM_CONFIGS["default"], "model", model)


def test_known_cap_clamps_purpose_want(_capture, monkeypatch):
    """apihub 声明 4096：edit-trace 的 16384 诉求必须被钳到上限。"""
    _use_gateway(monkeypatch, "https://apihub.agnes-ai.com/v1")
    cfg.get_llm(purpose="edit-trace")
    assert _capture["max_tokens"] == 4096


def test_uncapped_gateway_keeps_purpose_want(_capture, monkeypatch):
    """未声明上限的网关不干预（api.agnes-ai.cn 实测 16384 正常）。"""
    _use_gateway(monkeypatch, "https://api.agnes-ai.cn/v1")
    cfg.get_llm(purpose="edit-trace")
    assert _capture["max_tokens"] == 16384


def test_problem_budget_has_headroom():
    """problem 预算按历史峰值（3825/p99=2434）留余量，取 8192。"""
    assert cfg.PURPOSE_CONFIGS["problem"]["max_tokens"] == 8192


def test_problem_budget_clamped_on_capped_gateway(_capture, monkeypatch):
    """受限网关上仍按 4096 发（purpose 诉求再大也越不过网关上限）。"""
    _use_gateway(monkeypatch, "https://apihub.agnes-ai.com/v1")
    cfg.get_llm(purpose="problem")
    assert _capture["max_tokens"] == 4096


def test_problem_budget_kept_on_uncapped_gateway(_capture, monkeypatch):
    _use_gateway(monkeypatch, "https://api.agnes-ai.cn/v1")
    cfg.get_llm(purpose="problem")
    assert _capture["max_tokens"] == 8192


def test_learned_cap_applies_on_next_call(_capture, monkeypatch):
    _use_gateway(monkeypatch, "https://apihub.agnes-ai.com/v1", "agnes-2.5-flash")
    cfg.note_output_token_limit("https://apihub.agnes-ai.com/v1", "agnes-2.5-flash", 2048)
    cfg.get_llm(purpose="problem")
    assert _capture["max_tokens"] == 2048


def test_learned_cap_ignores_trailing_slash(_capture):
    cfg.note_output_token_limit("https://apihub.agnes-ai.com/v1/", "agnes-2.5-flash", 2048)
    assert cfg.output_token_limit("https://apihub.agnes-ai.com/v1", "agnes-2.5-flash") == 2048


def test_purpose_without_budget_is_untouched(_capture, monkeypatch):
    _use_gateway(monkeypatch, "https://apihub.agnes-ai.com/v1")
    cfg.get_llm(purpose="chat")
    assert "max_tokens" not in _capture
