"""测试用户（免注册试用）体系（2026-09-10，方案 v2 PR-B）。

覆盖：
- /auth/trial 建号：role='test'、占位邮箱、per-IP 建号限速
- /auth/claim 原地转正：id 不变、role→user、可登录、邀请码校验、邮箱冲突 409、
  普通用户 403、邮箱码路径（通道可用时）
- quota：IP 维度仅对 is_test 生效、测试用户触额走转化文案、reset_user 转正清桶
- settings：体验账号禁自定义 LLM（PUT/test 双端点 403）
- forgot-password：体验账号占位邮箱不发信
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_tutor_agent.api import auth as auth_mod
from code_tutor_agent.api import quota as quota_mod
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


def _invite(code: str = "TRIALIN1", max_uses: int = 5):
    dbmod.create_invite_code(code, max_uses, None)
    return code


def _make_trial(client) -> tuple[dict, str]:
    """建一个体验账号，返回 (user, token)。"""
    r = client.post("/auth/trial")
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"], body["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── /auth/trial ──


def test_trial_creation(client):
    """一键体验：role='test' + 占位邮箱；同浏览器重复请求各自建号（服务端不强制复用）。"""
    user, token = _make_trial(client)
    assert user["role"] == "test"
    assert user["email"].startswith("trial-") and user["email"].endswith("@trial.local")
    row = dbmod.get_user_by_id(user["id"])
    assert row["role"] == "test"

    user2, _ = _make_trial(client)
    assert user2["id"] != user["id"]


def test_trial_creation_rate_limit(client, monkeypatch):
    """per-IP 建号限速：阈值调到 2 后第 3 次 → 429（防脚本灌表）。"""
    monkeypatch.setenv("TRIAL_CREATE_RATE_LIMIT", "2")
    assert client.post("/auth/trial").status_code == 200
    assert client.post("/auth/trial").status_code == 200
    assert client.post("/auth/trial").status_code == 429


def test_trial_cannot_login(client):
    """体验账号密码随机不可知晓：任意密码登录一律 401。"""
    user, _ = _make_trial(client)
    r = client.post("/auth/login", json={"email": user["email"], "password": "password123"})
    assert r.status_code == 401


# ── /auth/claim ──


def test_claim_upgrades_in_place(client, monkeypatch):
    """转正：id 不变、role→user、可登录、占位邮箱被替换。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    user, token = _make_trial(client)
    invite = _invite("CLAIM001")

    r = client.post("/auth/claim", headers=_auth(token), json={
        "email": "real@test.com", "password": "password123",
        "confirm_password": "password123", "invite_code": invite,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["id"] == user["id"]          # 原地转正：id 不变
    assert body["user"]["role"] == "user"
    assert body["user"]["email"] == "real@test.com"

    row = dbmod.get_user_by_id(user["id"])
    assert row["role"] == "user" and row["email"] == "real@test.com"

    # 新密码可登录（旧占位身份不复存在）
    r2 = client.post("/auth/login", json={"email": "real@test.com", "password": "password123"})
    assert r2.status_code == 200
    assert r2.json()["user"]["id"] == user["id"]


def test_claim_with_email_code(client, monkeypatch):
    """邮件通道可用时：转正同样需要邮箱验证码（缺码 400，正确码 200）。"""
    import re

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: True)
    monkeypatch.setattr(f"{EMAIL_SVC}.send_email",
                        lambda to, subject, text: sent.append((to, text)) or True)
    user, token = _make_trial(client)
    invite = _invite("CLAIM002")

    payload = {"email": "coded@test.com", "password": "password123",
               "confirm_password": "password123", "invite_code": invite, "email_code": ""}
    r = client.post("/auth/claim", headers=_auth(token), json=payload)
    assert r.status_code == 400  # 缺验证码

    assert client.post("/auth/send-register-code", json={"email": "coded@test.com"}).status_code == 200
    code = re.search(r"(\d{6})", sent[-1][1]).group(1)
    payload["email_code"] = code
    r = client.post("/auth/claim", headers=_auth(token), json=payload)
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "user"


def test_claim_rejects_bad_invite(client, monkeypatch):
    """无效邀请码 → 400，且账号仍是 test（未发生部分变更）。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    user, token = _make_trial(client)
    r = client.post("/auth/claim", headers=_auth(token), json={
        "email": "x@test.com", "password": "password123",
        "confirm_password": "password123", "invite_code": "NOPE1234",
    })
    assert r.status_code == 400
    assert dbmod.get_user_by_id(user["id"])["role"] == "test"


def test_claim_rejects_normal_user_and_email_conflict(client, monkeypatch):
    """普通用户 403；邮箱被他人占用 409。"""
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: False)
    # 普通用户 → 403
    dbmod.create_user("normal@test.com", auth_mod.hash_password("password123"))
    r = client.post("/auth/login", json={"email": "normal@test.com", "password": "password123"})
    tok_normal = r.json()["token"]
    invite = _invite("CLAIM003")
    r = client.post("/auth/claim", headers=_auth(tok_normal), json={
        "email": "other@test.com", "password": "password123",
        "confirm_password": "password123", "invite_code": invite,
    })
    assert r.status_code == 403

    # 体验账号转正到已占用邮箱 → 409
    dbmod.create_user("occupied@test.com", auth_mod.hash_password("password123"))
    user, token = _make_trial(client)
    r = client.post("/auth/claim", headers=_auth(token), json={
        "email": "occupied@test.com", "password": "password123",
        "confirm_password": "password123", "invite_code": invite,
    })
    assert r.status_code == 409
    assert dbmod.get_user_by_id(user["id"])["role"] == "test"


# ── quota：IP 维度仅对测试用户生效 ──


@pytest.fixture()
def quota_on(monkeypatch):
    """打开配额总闸并收紧阈值；_client_ip 固定为 9.9.9.9（绕开 direct 短路）。"""
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "5")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "1")   # IP 维度阈值 1：一次即触顶
    monkeypatch.setattr(quota_mod, "_client_ip", lambda req: "9.9.9.9")


def test_quota_ip_dimension_only_for_test_users(quota_on):
    """is_test=True：同 IP 第二次做题即触顶（IP 桶共享）；is_test=False：不受 IP 维度影响。"""
    # 测试用户：第 2 次触顶，文案是转化钩子
    quota_mod.check_problem_start(None, "101", is_test=True)
    with pytest.raises(Exception) as ei:
        quota_mod.check_problem_start(None, "102", is_test=True)
    assert "注册" in str(getattr(ei.value, "detail", ""))

    # 普通用户：同 IP 多个用户各自 5 次以内都放行（IP 维度不生效）
    for uid in ("201", "202", "203"):
        quota_mod.check_problem_start(None, uid, is_test=False)
    # 非测试用户文案不含「注册」钩子（用普通用户桶验证一次触顶文案）
    monkey_over = quota_mod
    for _ in range(5):
        monkey_over.check_problem_start(None, "301", is_test=False)
    with pytest.raises(Exception) as ei2:
        monkey_over.check_problem_start(None, "301", is_test=False)
    assert "明天" in str(getattr(ei2.value, "detail", ""))


def test_trial_ip_anchor_route_level(client, monkeypatch):
    """路由级回归（2026-09-10 E2E 实测发现的存量 bug）。

    session.py 曾漏 import Request，叠加 `from __future__ import annotations`
    把注解字符串化，FastAPI 运行期解析失败后静默注入 request=None，
    _client_ip(None)=='direct' → IP 维度在线上从未生效。
    本测试走真实路由 + 真实 _client_ip（TestClient host='testclient'），
    锁死 Request 注入行为；上面的单元测试 patch 掉了 _client_ip，抓不到这类问题。
    """
    from code_tutor_agent.api.routers import session as session_mod

    # 拦掉后台生成，避免测试触发真实 LLM
    monkeypatch.setattr(session_mod, "run_generation", lambda *a, **k: None)
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "0")  # 关用户维度，只测 IP 锚点
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "1")
    quota_mod.reset_all()
    try:
        _, token = _make_trial(client)
        body = {"topic": "数组", "difficulty": "easy"}
        r1 = client.post("/session", json=body, headers=_auth(token))
        assert r1.status_code == 200, r1.text  # IP 桶 1/1
        r2 = client.post("/session", json=body, headers=_auth(token))
        assert r2.status_code == 429, f"IP 锚点未生效（request 注入回归？）: {r2.text}"
        assert "注册" in r2.json()["detail"]
    finally:
        quota_mod.reset_all()


def test_reset_user_after_claim(quota_on):
    """转正清桶：uid 触顶后 reset_user 重新开闸；IP 桶不被误清（仍对 test 生效）。"""
    quota_mod.check_problem_start(None, "401", is_test=True)  # IP 桶 1 次已满
    with pytest.raises(Exception):
        quota_mod.check_problem_start(None, "401", is_test=True)
    # 清该用户桶后：用户桶可用，但 IP 桶仍满 → 测试用户仍被拒（IP 锚不清）
    quota_mod.reset_user("401")
    with pytest.raises(Exception):
        quota_mod.check_problem_start(None, "401", is_test=True)
    # 普通用户不受 IP 桶影响：转正后（is_test=False）立刻可用
    quota_mod.check_problem_start(None, "401", is_test=False)


# ── settings：体验账号禁自定义 LLM ──


def test_settings_reject_custom_for_trial(client, monkeypatch):
    """PUT 与 test 端点对 role='test' 一律 403（即便 allow_custom 总闸开着）。"""
    _, token = _make_trial(client)
    h = _auth(token)
    body = {"mode": "custom", "model": "m", "base_url": "https://api.example.com/v1",
            "api_key": "sk-test"}
    assert client.put("/settings/me", headers=h, json=body).status_code == 403
    assert client.post("/settings/me/test", headers=h, json=body).status_code == 403
    # default 模式不受限
    assert client.put("/settings/me", headers=h,
                      json={"mode": "default"}).status_code == 200


def test_settings_custom_allowed_for_normal(client, monkeypatch):
    """对照：普通用户在 allow_custom 开启时可用 custom（确认 403 只针对 test 角色）。"""
    monkeypatch.setenv("CTA_ALLOW_CUSTOM_LLM", "1")
    dbmod.create_user("nor@test.com", auth_mod.hash_password("password123"))
    tok = client.post("/auth/login", json={
        "email": "nor@test.com", "password": "password123"}).json()["token"]
    r = client.put("/settings/me", headers=_auth(tok), json={
        "mode": "custom", "model": "m", "base_url": "https://api.example.com/v1",
        "api_key": "sk-test"})
    assert r.status_code == 200


# ── forgot-password：体验账号不发信 ──


def test_forgot_password_skips_trial_placeholder(client, monkeypatch):
    """体验账号占位邮箱：走统一措辞，不真正发信（不浪费邮件额度）。"""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(f"{EMAIL_SVC}.is_configured", lambda: True)
    monkeypatch.setattr(f"{EMAIL_SVC}.send_email",
                        lambda to, subject, text: sent.append((to, text)) or True)
    user, _ = _make_trial(client)
    r = client.post("/auth/forgot-password", json={"email": user["email"]})
    assert r.status_code == 200
    assert len(sent) == 0  # 占位邮箱不发信
