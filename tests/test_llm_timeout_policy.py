"""LLM 请求超时上界（config.get_llm，2026-10-27 submit 延迟治理）。

回归要防的是「某条链路又回到无超时」：openai SDK 默认 600s + 自动重试 2 次，一次网关
挂住就是一次分钟级白屏，而 submit 的辅导文案当时正串在判题后面。这里断言的是**上界
本身**，不是文案质量。
"""
from __future__ import annotations

import pytest

from code_tutor_agent.config import get_llm


def _clear_llm_env(monkeypatch):
    for k in ("LLM_TIMEOUT_SECONDS", "LLM_TIMEOUT_JUDGE_SECONDS", "LLM_MAX_RETRIES"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_llm_env(monkeypatch)
    yield


def test_every_purpose_gets_a_timeout_bound():
    """不允许任何一个用途落在 SDK 的 600s 默认上——包括没在按用途表里列出的。"""
    for purpose in ("judge", "tutor-generate", "edit-trace", "api-chat", "memory-extract"):
        llm = get_llm(purpose=purpose)
        assert llm.request_timeout, f"{purpose} 没有超时上界"
        assert float(llm.request_timeout) <= 90.0


def test_request_path_purpose_is_tighter():
    """judge 的结论用户正在请求里等（AC 线与后台段都沿用这个用途），给更紧的额度。"""
    assert float(get_llm(purpose="judge").request_timeout) == 45.0
    assert float(get_llm(purpose="dialog").request_timeout) == 90.0


def test_max_retries_is_bounded():
    assert get_llm(purpose="judge").max_retries == 1


def test_env_can_adjust_without_a_code_change(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_JUDGE_SECONDS", "12")
    assert float(get_llm(purpose="judge").request_timeout) == 12.0


def test_caller_override_still_wins():
    """用途默认只兜底，显式传参仍是最终决定权（与用户覆盖 key 的优先级口径一致）。"""
    assert float(get_llm(purpose="judge", timeout=7).request_timeout) == 7.0
