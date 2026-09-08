"""用户敏感配置（自定义 LLM API key）的加密落库工具。

- 对称加密用 Fernet（AES-128-CBC + HMAC-SHA256，由 cryptography 提供）。
- 密钥派生：优先环境变量 ``CTA_SECRET_KEY``；否则复用 ``JWT_SECRET``；
  再否则回退读取 auth 持久化的 ``data/db/.jwt_secret``（与 JWT 同密钥，
  保证多副本统一、无需新增密钥管理）。任意长度 secret 经 SHA-256
  派生为 32 字节 Fernet key。
- 密文统一加 ``enc::`` 前缀，便于与存量明文区分，且 encrypt 对密文幂等。
- 密钥缺失时降级为明文存储并告警（不抛异常，避免启动崩溃）；
  解密时遇到非 ``enc::`` 前缀的值原样返回，兼容存量明文。

仅用于 ``user_settings.llm_api_key``。调用方：``db/database.py`` 的
``save_user_settings``（入库前 encrypt）/ ``get_user_settings``（出库后 decrypt）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_PREFIX = "enc::"

# 回退密钥文件路径（与 api/auth.py 的 JWT secret 持久化位置一致）
_JWT_SECRET_FILE = Path(__file__).resolve().parents[3] / "data" / "db" / ".jwt_secret"

# 缓存 Fernet 实例；哨兵区分"未初始化"与"降级(None)"
_SENTINEL = object()
_FERNET_CACHE = _SENTINEL


def _resolve_secret() -> str:
    """按优先级解析加密密钥原文：CTA_SECRET_KEY → JWT_SECRET → .jwt_secret 文件。"""
    secret = (os.getenv("CTA_SECRET_KEY") or "").strip()
    if secret:
        return secret
    secret = (os.getenv("JWT_SECRET") or "").strip()
    if secret:
        return secret
    try:
        return (_JWT_SECRET_FILE.read_text(encoding="utf-8") or "").strip()
    except OSError:
        return ""


def _get_fernet() -> Fernet | None:
    """派生并返回 Fernet 实例；密钥缺失返回 None（调用方降级为明文）。"""
    global _FERNET_CACHE
    if _FERNET_CACHE is not _SENTINEL:
        return _FERNET_CACHE  # type: ignore[return-value]
    secret = _resolve_secret()
    if not secret:
        logger.warning(
            "未配置加密密钥(CTA_SECRET_KEY/JWT_SECRET)，用户 API key 将以明文存储"
        )
        _FERNET_CACHE = None
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    _FERNET_CACHE = Fernet(key)
    return _FERNET_CACHE


def encrypt_secret(plain: str) -> str:
    """加密明文 secret。

    - 空串原样返回；
    - 已加密（``enc::`` 前缀）幂等返回，避免双重加密；
    - 无密钥时降级返回明文（配合告警）。
    """
    if not plain:
        return ""
    if plain.startswith(_PREFIX):
        return plain
    fernet = _get_fernet()
    if fernet is None:
        return plain
    return _PREFIX + fernet.encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    """解密 token。

    - 空串原样返回；
    - 非 ``enc::`` 前缀视为存量明文，原样返回；
    - 密钥不匹配/数据损坏导致解密失败时告警并返回原值（不抛异常）。
    """
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        return token
    fernet = _get_fernet()
    if fernet is None:
        return token
    try:
        return fernet.decrypt(token[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.warning("API key 解密失败(密钥不匹配或数据损坏)，返回原值")
        return token


def reset_key_cache() -> None:
    """测试用：清空密钥/Fernet 缓存，使下次调用重新解析环境变量与文件。"""
    global _FERNET_CACHE
    _FERNET_CACHE = _SENTINEL
