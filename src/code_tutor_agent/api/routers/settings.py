"""用户设置路由 — 自定义 LLM 接入（API key / base URL / model）。

- 设置按用户隔离存 SQLite（user_settings 表，见 db/database.py）。
- llm_mode: "default"=跟随服务器 .env 配置；"custom"=用户自定义（OpenAI 兼容接口）。
- 完整 API key 永不回传前端：GET 只回打码形式；PUT 留空表示沿用已保存的 key。
- 生效路径：main.py 中间件按 Bearer token 解出 user_id → load_user_llm_cfg →
  runtime_settings ContextVar → config.get_llm() 采用覆盖值。内存缓存按用户失效。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from code_tutor_agent.api.auth import get_current_user
from code_tutor_agent.db.database import (
    delete_user_settings,
    get_user_settings,
    save_user_settings,
)
from code_tutor_agent.config import get_allow_custom_llm
from code_tutor_agent.runtime_settings import build_override

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 进程内缓存：中间件每请求都要查设置，SQLite 查询虽快也省掉 ──
# key=user_id, value=覆盖 dict（custom 模式）或 None（默认模式）。写操作即时失效。
_cfg_cache: dict[int, dict[str, str] | None] = {}
_cfg_lock = threading.Lock()


def load_user_llm_cfg(user_id: int) -> dict[str, str] | None:
    """中间件入口：该用户的 LLM 覆盖配置（default 模式/无记录 → None）。"""
    with _cfg_lock:
        if user_id in _cfg_cache:
            return _cfg_cache[user_id]
    try:
        row = get_user_settings(user_id)
    except Exception:
        logger.exception("load user llm settings failed: user=%s", user_id)
        row = None
    cfg = build_override(row["llm_model"], row["llm_base_url"], row["llm_api_key"]) \
        if row and row.get("llm_mode") == "custom" else None
    with _cfg_lock:
        _cfg_cache[user_id] = cfg
    return cfg


def invalidate_user_llm_cfg(user_id: int) -> None:
    """PUT/DELETE 设置后失效缓存，下一次请求即生效。"""
    with _cfg_lock:
        _cfg_cache.pop(user_id, None)


# ── 请求/响应模型 ──

class LlmSettingsBody(BaseModel):
    mode: str = Field("default", description="default=跟随服务器配置; custom=自定义")
    model: str = Field("", max_length=200, description="模型名（custom 必填）")
    base_url: str = Field("", max_length=500, description="OpenAI 兼容 base URL（custom 必填）")
    api_key: str = Field("", max_length=500, description="留空=沿用已保存的 key")


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-4:]


# ── SSRF 校验（审计 F-02，2026-09-08）─────────────────────────────
# 用户自定义 base_url 会被服务端直接请求（get_llm / test 端点），不校验即
# SSRF：可打云 metadata（169.254.169.254）、探内网（app-db:5432 等）。
# 两档策略：
#   默认档（本地开发）：仅拦 metadata / 链路本地段 —— 兼容本机 Ollama 等
#     http://localhost:11434 这类合法本地网关；
#   生产档（CTA_ENV=production 或 CTA_SSRF_BLOCK_PRIVATE=1）：私网/环回/
#     链路本地全部禁止，并对域名做 DNS 解析二次校验（解析到私网 IP 也拒）。

_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "metadata"}


def _ip_is_dangerous(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified or ip.is_multicast
    )


def _host_literal_dangerous(hostname: str) -> bool:
    """字面量判定（不做 DNS）：metadata 域名 + IP 字面量。"""
    h = (hostname or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in _METADATA_HOSTS or h.endswith(".internal") or h.endswith(".localhost"):
        return True
    try:
        return _ip_is_dangerous(ipaddress.ip_address(h))
    except ValueError:
        return h == "localhost"


def _host_resolves_dangerous(hostname: str) -> bool:
    """DNS 解析二次校验：任一解析结果落在保留/私网段即拒绝。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return True  # 解析失败按不信任处理
    for info in infos:
        try:
            if _ip_is_dangerous(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False


def _ssrf_strict() -> bool:
    if (os.getenv("CTA_ENV", "").strip().lower() == "production"):
        return True
    return os.getenv("CTA_SSRF_BLOCK_PRIVATE", "0").strip().lower() in ("1", "true", "yes", "on")


def _validate_base_url(base_url: str) -> None:
    """校验用户自定义 LLM base_url；不合法抛 400（含 SSRF 拦截）。"""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Base URL 必须以 http:// 或 https:// 开头")
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise HTTPException(400, "Base URL 缺少主机名")

    if _ssrf_strict():
        # 生产档：私网/环回/metadata 全禁 + DNS 解析二次校验
        if _host_literal_dangerous(hostname) or _host_resolves_dangerous(hostname):
            raise HTTPException(400, "Base URL 不允许指向内网/本地/metadata 地址")
        return

    # 开发档：放行本机网关（如 http://localhost:11434 的 Ollama），
    # 但 metadata 域名与链路本地段（169.254.0.0/16 云 metadata）仍然拦截
    if hostname in _METADATA_HOSTS or hostname.endswith(".internal") or hostname.endswith(".localhost"):
        raise HTTPException(400, "Base URL 不允许指向 metadata 地址")
    try:
        if ipaddress.ip_address(hostname).is_link_local:
            raise HTTPException(400, "Base URL 不允许指向链路本地地址")
    except ValueError:
        return  # 公网域名，放行


def _payload(row: dict | None) -> dict:
    allow_custom = get_allow_custom_llm()
    if not row or row.get("llm_mode") != "custom":
        return {"mode": "default", "model": "", "base_url": "",
                "api_key_masked": "", "has_custom": False,
                "allow_custom": allow_custom}
    return {
        "mode": "custom",
        "model": row.get("llm_model") or "",
        "base_url": row.get("llm_base_url") or "",
        "api_key_masked": _mask(row.get("llm_api_key") or ""),
        "has_custom": True,
        "allow_custom": allow_custom,
    }


def _resolve_custom_cfg(body: LlmSettingsBody, user_id: int) -> dict[str, str]:
    """组装 custom 模式配置；api_key 留空时沿用库里已存的。"""
    api_key = body.api_key
    if not api_key:
        existing = get_user_settings(user_id) or {}
        api_key = existing.get("llm_api_key") or ""
    model = body.model.strip()
    base_url = body.base_url.strip().rstrip("/")
    if not model or not base_url or not api_key.strip():
        raise HTTPException(400, "自定义模式下 模型名称 / Base URL / API key 均必填")
    _validate_base_url(base_url)  # SSRF 校验（F-02）：PUT 与 test 端点共用此入口
    return {"model": model, "base_url": base_url, "api_key": api_key.strip()}


# ── 路由 ──

@router.get("/me")
async def read_settings(current: dict = Depends(get_current_user)):
    """当前用户的 LLM 设置（key 打码）。"""
    return _payload(get_user_settings(current["id"]))


@router.put("/me")
async def update_settings(body: LlmSettingsBody, current: dict = Depends(get_current_user)):
    """保存设置。custom 模式三项必填（key 可沿用旧值）；default 模式删除自定义记录。"""
    uid = current["id"]
    mode = body.mode if body.mode in ("default", "custom") else "default"
    if mode == "custom" and current.get("role") == "test":
        raise HTTPException(403, "体验账号不支持自定义 API key，注册正式账号后即可使用")
    if mode == "custom" and not get_allow_custom_llm():
        raise HTTPException(403, "管理员已关闭自定义 API key 功能，请使用系统默认模型")
    try:
        if mode == "default":
            delete_user_settings(uid)
        else:
            cfg = _resolve_custom_cfg(body, uid)
            save_user_settings(uid, mode="custom", **cfg)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("save user settings failed: user=%s", uid)
        raise HTTPException(500, "保存失败，请稍后重试")
    invalidate_user_llm_cfg(uid)
    logger.info("user settings updated: user=%s mode=%s", uid, mode)
    return _payload(get_user_settings(uid))


@router.post("/me/test")
async def test_settings(body: LlmSettingsBody, current: dict = Depends(get_current_user)):
    """连通性测试：用给定配置（不落库）发一次最小补全。

    custom 模式 key 留空时沿用已保存的 key，方便只改模型名时直接测试。
    """
    # 频控（F-02 配套）：test 端点会让服务端向用户给定 URL 发请求，
    # 不限频即可被用来高频探测内网。每用户每小时 10 次。
    from code_tutor_agent.api.auth import rate_limit

    rate_limit(f"llmtest:{current['id']}", 10, 3600)
    if body.mode == "custom" and current.get("role") == "test":
        raise HTTPException(403, "体验账号不支持自定义 API key，注册正式账号后即可使用")
    if body.mode == "custom" and not get_allow_custom_llm():
        raise HTTPException(403, "管理员已关闭自定义 API key 功能，无法测试自定义模型")
    if body.mode == "custom":
        cfg = _resolve_custom_cfg(body, current["id"])
    else:
        from code_tutor_agent.config import LLM_CONFIGS
        d = LLM_CONFIGS.get("default") or {}
        cfg = {"model": d.get("model") or "", "base_url": d.get("base_url") or "",
               "api_key": d.get("api_key") or ""}
        if not cfg["model"] or not cfg["api_key"]:
            raise HTTPException(400, "服务器默认模型未配置，请检查 .env 的 LLM_MODEL / LLM_API_KEY")
    from langchain.chat_models import init_chat_model
    try:
        llm = init_chat_model(
            model=cfg["model"], model_provider="openai",
            base_url=cfg["base_url"], api_key=cfg["api_key"], max_tokens=16,
        )
        reply = llm.invoke("请只回复两个字：正常")
        text = getattr(reply, "content", "") or ""
        return {"ok": True, "sample": str(text)[:60]}
    except Exception as exc:
        logger.warning("llm settings test failed: user=%s err=%s", current["id"], exc)
        return {"ok": False, "error": str(exc)[:300]}
