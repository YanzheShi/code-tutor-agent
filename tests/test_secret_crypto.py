"""用户 API key 加密落库工具（security/secret_crypto.py）单元测试。

不依赖数据库，纯验证加解密/降级/兼容逻辑。
"""
from pathlib import Path

import pytest

from code_tutor_agent.security import secret_crypto as sc


@pytest.fixture(autouse=True)
def _reset_keys(monkeypatch):
    """每个用例前清空密钥相关 env 与模块缓存，避免相互污染。"""
    monkeypatch.delenv("CTA_SECRET_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    sc.reset_key_cache()
    yield
    sc.reset_key_cache()


def test_roundtrip_with_cta_secret_key(monkeypatch):
    monkeypatch.setenv("CTA_SECRET_KEY", "super-secret-passphrase")
    plain = "sk-1234567890abcdef"
    enc = sc.encrypt_secret(plain)
    assert enc != plain
    assert enc.startswith("enc::")
    assert sc.decrypt_secret(enc) == plain


def test_reuses_jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "jwt-shared-secret")
    plain = "sk-abcdef"
    enc = sc.encrypt_secret(plain)
    assert enc.startswith("enc::")
    assert sc.decrypt_secret(enc) == plain


def test_encrypt_is_idempotent(monkeypatch):
    monkeypatch.setenv("CTA_SECRET_KEY", "k")
    enc = sc.encrypt_secret("sk-x")
    enc2 = sc.encrypt_secret(enc)  # 已加密再加密应幂等
    assert enc == enc2
    assert sc.decrypt_secret(enc2) == "sk-x"


def test_empty_string_passthrough():
    assert sc.encrypt_secret("") == ""
    assert sc.decrypt_secret("") == ""


def test_legacy_plaintext_passthrough():
    # 存量明文（无 enc:: 前缀）原样返回，不报错
    assert sc.decrypt_secret("sk-plain-old") == "sk-plain-old"


def test_no_key_degrades_to_plaintext(monkeypatch):
    # 无密钥且无可回退文件：encrypt 返回明文，decrypt 原样返回
    monkeypatch.setattr(sc, "_JWT_SECRET_FILE", Path("/nope/.jwt_secret"))
    enc = sc.encrypt_secret("sk-nokey")
    assert enc == "sk-nokey"
    assert sc.decrypt_secret(enc) == "sk-nokey"


def test_wrong_key_fails_gracefully(monkeypatch):
    monkeypatch.setenv("CTA_SECRET_KEY", "right-key")
    enc = sc.encrypt_secret("sk-secret")
    monkeypatch.setenv("CTA_SECRET_KEY", "wrong-key")
    sc.reset_key_cache()
    # 解密失败（InvalidToken）→ 返回原值，不抛异常
    assert sc.decrypt_secret(enc) == enc
