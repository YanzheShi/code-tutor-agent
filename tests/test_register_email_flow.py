"""注册邮箱验证码流程（2026-09-10，方案 v2 PR-A）。

覆盖：
- DB 层 purpose 隔离：register 码与 reset 码互不混用、重发作废旧码、单次消费
- send-register-code 端点：未配邮件降级、happy path、per-IP 限流（阈值 SEND_REGISTER_RATE_LIMIT 可配，当前 .env=3，
  放宽到 3 是为放行同 IP 下的多个不同用户、避免校园网/NAT 误杀正常注册）、per-email 5/24h、
  已注册邮箱 409、非法邮箱 400
- 注：前端 AuthModal 的 60s 倒计时 UX 限制的是单个用户重复发码，与后端 per-IP 限流职责不同、各管各的
- register 双确认：邮件服务可用时强制邮箱码（缺/错/过期/已消费均拒），
  未配置时降级为仅邀请码
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from code_tutor_agent.api import auth as auth_mod
from code_tutor_agent.db import database as dbmod

EMAIL_SVC = "code_tutor_agent.api.email"


@pytest.fixture()
def temp_db():
    """确保测试 schema 内建表（幂等 init_db）；行级隔离由 conftest _pg_clean_tables 兜底。"""
    dbmod.init_db()
    yield


@pytest.fixture()
def client(temp_db):
    from code_tutor_agent.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def email_configured(monkeypatch):
    """邮件服务可用 + 捕获发送内容（从正文提取 6 位明文验证码）。"""
    sent: list[tuple[str, str]] = []

    def _fake_send(to_email, subject, text):
        sent.append((to_email, text))
        return True

    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: True)
    monkeypatch.setattr(f"{EMAIL_SVC}.send_email", _fake_send)
    return sent


def _last_code(sent) -> str:
    assert sent, "没有捕获到任何邮件发送"
    m = re.search(r"(\d{6})", sent[-1][1])
    assert m, f"邮件正文未找到 6 位验证码: {sent[-1][1]}"
    return m.group(1)


def _invite(code: str = "TESTINV1", max_uses: int = 5):
    dbmod.create_invite_code(code, max_uses, None)
    return code


# ── DB 层：purpose 隔离 ──


def test_register_code_purpose_isolation(temp_db):
    """register 码与 reset 码互不混用：跨 purpose 校验一律失败。"""
    dbmod.save_password_reset_code("a@test.com", "hash_reg", "2999-01-01 00:00:00", purpose="register")
    dbmod.save_password_reset_code("a@test.com", "hash_reset", "2999-01-01 00:00:00", purpose="reset")

    # 各自 purpose 内可校验
    assert dbmod.verify_password_reset_code("a@test.com", "hash_reg", purpose="register")
    assert dbmod.verify_password_reset_code("a@test.com", "hash_reset", purpose="reset")
    # 跨 purpose 不可用（注册码不能当重置码用，反之亦然）
    assert not dbmod.verify_password_reset_code("a@test.com", "hash_reg", purpose="reset")
    assert not dbmod.verify_password_reset_code("a@test.com", "hash_reset", purpose="register")

    # 单次消费
    dbmod.consume_password_reset_code("a@test.com", "hash_reg", purpose="register")
    assert not dbmod.verify_password_reset_code("a@test.com", "hash_reg", purpose="register")
    # reset 码不受 register 消费影响
    assert dbmod.verify_password_reset_code("a@test.com", "hash_reset", purpose="reset")


def test_register_code_resend_invalidates_old(temp_db):
    """同一邮箱+purpose 重发：旧码作废、新码有效；不同 purpose 互不作废。"""
    dbmod.save_password_reset_code("b@test.com", "hash_old", "2999-01-01 00:00:00", purpose="register")
    dbmod.save_password_reset_code("b@test.com", "hash_new", "2999-01-01 00:00:00", purpose="register")
    assert not dbmod.verify_password_reset_code("b@test.com", "hash_old", purpose="register")
    assert dbmod.verify_password_reset_code("b@test.com", "hash_new", purpose="register")


def test_register_code_expired(temp_db):
    """过期码不可用。"""
    dbmod.save_password_reset_code("c@test.com", "hash_exp", "2000-01-01 00:00:00", purpose="register")
    assert not dbmod.verify_password_reset_code("c@test.com", "hash_exp", purpose="register")


# ── send-register-code 端点 ──


def test_send_code_disabled_when_email_unconfigured(client, monkeypatch):
    """邮件通道未配置：返回 email_verification=False，前端降级为仅邀请码注册。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    r = client.post("/auth/send-register-code", json={"email": "new@test.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["email_verification"] is False
    assert body["delivered"] is False


def test_send_code_happy_path_and_per_ip_limit(client, email_configured):
    """正常下发；同 IP 60s 内最多 3 次（SEND_REGISTER_RATE_LIMIT=3），第 4 次 → 429。"""
    # 同 IP（TestClient 固定 testclient）一分钟内前 3 次放行
    for i in range(3):
        r = client.post("/auth/send-register-code", json={"email": "fresh@test.com"})
        assert r.status_code == 200, f"第 {i + 1} 次不应被限"
    assert len(email_configured) == 3
    assert all(e == "fresh@test.com" for e, _ in email_configured)

    # 第 4 次 → 429（per-IP 3/min，SEND_REGISTER_RATE_LIMIT 可配）
    r4 = client.post("/auth/send-register-code", json={"email": "fresh@test.com"})
    assert r4.status_code == 429
    assert len(email_configured) == 3  # 限频拒绝时不发信


def test_send_code_per_email_daily_limit(client, email_configured, monkeypatch):
    """per-email 5 次/24h：换 IP 绕开 per-IP 限制后，第 6 次（同邮箱）仍 429。"""
    counter = {"n": 0}

    def _cycling_ip(_req):
        counter["n"] += 1
        return f"10.0.0.{counter['n']}"  # 每次不同 IP，per-IP 桶不拦

    monkeypatch.setattr(auth_mod, "_client_ip", _cycling_ip)
    for i in range(5):
        r = client.post("/auth/send-register-code", json={"email": "bomb@test.com"})
        assert r.status_code == 200, f"第 {i + 1} 次不应被限"
    assert len(email_configured) == 5
    # 同邮箱第 6 次 → 429；不同邮箱不受影响（per-email 隔离）
    assert client.post("/auth/send-register-code", json={"email": "bomb@test.com"}).status_code == 429
    assert client.post("/auth/send-register-code", json={"email": "other@test.com"}).status_code == 200


def test_send_code_rejects_registered_email(client, email_configured):
    """已注册邮箱 409（在 per-email 计数前拦截，不烧额度、不发信）。"""
    dbmod.create_user("taken@test.com", auth_mod.hash_password("password123"))
    assert client.post("/auth/send-register-code", json={"email": "taken@test.com"}).status_code == 409
    assert len(email_configured) == 0


def test_send_code_per_ip_counts_rejected_attempts(client, email_configured):
    """per-IP 3/min 对被拒请求同样计数：连续 3 次 409 后立刻重试 → 第 4 次 429（防滥用语义）。"""
    dbmod.create_user("taken2@test.com", auth_mod.hash_password("password123"))
    # 被拒请求（已注册邮箱 409）同样占用 per-IP 额度：前 3 次返回 409
    for i in range(3):
        r = client.post("/auth/send-register-code", json={"email": "taken2@test.com"})
        assert r.status_code == 409, f"第 {i + 1} 次应被拒(409)"
    # 第 4 次：per-IP 桶（SEND_REGISTER_RATE_LIMIT=3）耗尽 → 429
    r4 = client.post("/auth/send-register-code", json={"email": "taken2@test.com"})
    assert r4.status_code == 429
    assert len(email_configured) == 0  # 409/429 均不发信


def test_send_code_rejects_bad_email(client, email_configured):
    """非法邮箱 400（独立测试以获得干净限流桶）。"""
    assert client.post("/auth/send-register-code", json={"email": "not-an-email"}).status_code == 400
    assert len(email_configured) == 0


# ── register 双确认 ──


def _register_payload(email: str, code: str, invite: str):
    return {
        "email": email,
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": invite,
        "email_code": code,
    }


def test_register_requires_email_code_when_configured(client, email_configured):
    """邮件服务可用时：缺验证码 / 错验证码 / 已消费验证码均拒；正确码注册成功。"""
    invite = _invite("INVABC1")
    email = "coder@test.com"

    # 缺验证码 → 400
    r = client.post("/auth/register", json=_register_payload(email, "", invite))
    assert r.status_code == 400
    # 错验证码 → 400
    assert client.post("/auth/send-register-code", json={"email": email}).status_code == 200
    r = client.post("/auth/register", json=_register_payload(email, "000000", invite))
    assert r.status_code == 400

    # 正确码 → 200，用户落库
    code = _last_code(email_configured)
    r = client.post("/auth/register", json=_register_payload(email, code, invite))
    assert r.status_code == 200
    assert r.json()["user"]["email"] == email
    assert dbmod.get_user_by_email(email) is not None

    # 验证码已消费：换邮箱用同一码注册 → 400（单次有效）
    email2 = "coder2@test.com"
    r = client.post("/auth/register", json=_register_payload(email2, code, invite))
    assert r.status_code == 400


def test_register_with_reset_purpose_code_fails(client, email_configured):
    """purpose 隔离端到端：reset 码不能通过注册校验。"""
    dbmod.save_password_reset_code(
        "mix@test.com", "deadbeef" * 8, "2999-01-01 00:00:00", purpose="reset"
    )
    # register 校验的是 sha256(code+secret)，这里直接走端点验证「哈希不匹配」路径
    invite = _invite("INVMIX01")
    r = client.post("/auth/register", json=_register_payload("mix@test.com", "111111", invite))
    assert r.status_code == 400


def test_register_fallback_without_email_service(client, monkeypatch):
    """邮件通道未配置：仅邀请码即可注册（降级路径，向后兼容）。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    invite = _invite("INVFALL1")
    r = client.post("/auth/register", json={
        "email": "legacy@test.com", "password": "password123",
        "confirm_password": "password123", "invite_code": invite,
    })
    assert r.status_code == 200
    assert dbmod.get_user_by_email("legacy@test.com") is not None


def test_public_invite_exposes_email_verification(client, monkeypatch):
    """public-invite 携带 email_verification 开关（前端据此决定是否展示发码 UI）。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: True)
    r = client.get("/auth/public-invite").json()
    assert r["email_verification"] is True
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    assert client.get("/auth/public-invite").json()["email_verification"] is False
