"""自定义 LLM API key 功能总开关（CTA_ALLOW_CUSTOM_LLM）单元测试。

默认关闭：不允许用户自定义 key；置 1 开启。开关在 GET /settings/me 的 payload
（allow_custom 字段）与 PUT/POST test 的拦截中体现。
"""
import pytest
from fastapi import HTTPException

from code_tutor_agent.config import get_allow_custom_llm
from code_tutor_agent.api.routers.settings import LlmSettingsBody, _payload, update_settings


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CTA_ALLOW_CUSTOM_LLM", raising=False)
    yield


def test_allow_custom_llm_default_off():
    assert get_allow_custom_llm() is False


def test_allow_custom_llm_on(monkeypatch):
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "1")
    assert get_allow_custom_llm() is True


def test_payload_reflects_flag(monkeypatch):
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "0")
    assert _payload(None)["allow_custom"] is False
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "1")
    assert _payload(None)["allow_custom"] is True


@pytest.mark.asyncio
async def test_update_custom_blocked_when_disabled(monkeypatch):
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "0")
    body = LlmSettingsBody(mode="custom", model="m", base_url="https://x", api_key="k")
    with pytest.raises(HTTPException) as exc:
        await update_settings(body, {"id": 1})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_custom_allowed_when_enabled(monkeypatch):
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "1")
    # 开关放行时不应在拦截处提前 403；走到参数校验才会因空字段抛 400。
    body = LlmSettingsBody(mode="custom", model="", base_url="", api_key="")
    try:
        await update_settings(body, {"id": 1})
    except HTTPException as exc:
        assert exc.status_code != 403
