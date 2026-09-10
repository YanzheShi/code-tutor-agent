"""多用户改造回归测试（P1/P2/P3 核心链路，不依赖 LLM / HTTP 服务）。

覆盖：
- 密码哈希 / 校验（PBKDF2）
- users 表 CRUD + 首个用户 admin 引导判定
- JWT 签发 / 解码 / 篡改拒绝 / get_current_user 依赖
- 会话归属（touch_session / get_session_owner / get_session_ids_for_user）
- 提交按用户隔离（save_submission / get_submissions_by_problem / get_all_problem_verdicts）
- build_run_config 的 user_id 贯穿（configurable + metadata）
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.security.http import HTTPAuthorizationCredentials

from code_tutor_agent.api import auth as auth_mod
from code_tutor_agent.db import database as dbmod
from code_tutor_agent.observability import build_run_config


@pytest.fixture(autouse=True)
def _email_service_offline(monkeypatch):
    """本文件统一跑「邮件通道未配置」降级口径（注册=仅邀请码）。

    2026-09-10 起注册接入邮箱验证码（email_svc.is_configured() 可用时强制），
    而测试进程经 conftest load_dotenv 会带上 MCP_HUB_* 使其默认为 True——
    把全部旧注册测试打回降级路径；邮箱验证码流程由
    test_register_email_flow.py 专项覆盖（含配置可用/不可用两态）。
    """
    monkeypatch.setattr("code_tutor_agent.api.email.is_configured", lambda: False)


@pytest.fixture()
def temp_db():
    """把 DB_PATH 指到临时库并初始化，测完还原。"""
    orig = dbmod.DB_PATH
    fd, tpath = tempfile.mkstemp(suffix=".db", prefix="cta_auth_test_")
    os.close(fd)
    os.unlink(tpath)
    dbmod.DB_PATH = tpath
    dbmod.init_db()
    yield tpath
    dbmod.DB_PATH = orig
    try:
        os.unlink(tpath)
    except OSError:
        pass


# ── 密码哈希 ──


def test_password_hash_roundtrip():
    h = auth_mod.hash_password("s3cret-password")
    assert h.startswith("pbkdf2_sha256$")
    assert auth_mod.verify_password("s3cret-password", h)
    assert not auth_mod.verify_password("wrong", h)


def test_password_verify_garbage_stored():
    assert not auth_mod.verify_password("x", "not-a-valid-hash")
    assert not auth_mod.verify_password("x", "")


# ── users 表 ──


def test_create_and_get_user(temp_db):
    uid = dbmod.create_user("a@test.com", "hash-x", role="user")
    user = dbmod.get_user_by_email("a@test.com")
    assert user and user["id"] == uid and user["role"] == "user"
    assert dbmod.get_user_by_email("A@TEST.COM") is None  # SQLite TEXT 比较区分大小写，归一在调用方（register lower）
    assert dbmod.get_user_by_id(uid)["email"] == "a@test.com"
    assert dbmod.get_user_by_email("nobody@test.com") is None


def test_duplicate_email_raises_integrity(temp_db):
    dbmod.create_user("dup@test.com", "h1")
    with pytest.raises(Exception) as exc:
        dbmod.create_user("dup@test.com", "h2")
    # 断言约束名而非错误文案：PG 服务端 lc_messages 本地化后文案是中文（"重复键违反唯一约束"）
    assert "users_email_key" in str(exc.value)


def test_bootstrap_admin_idempotent(temp_db):
    """管理员由启动脚本直接分配（ensure_bootstrap_admin），注册不再自举 admin。"""
    assert not dbmod.has_admin()
    auth_mod.ensure_bootstrap_admin()
    user = dbmod.get_user_by_email("534629255@qq.com")
    assert user is not None and user["role"] == "admin"
    assert dbmod.has_admin()
    # 幂等：重复调用不重复建号
    auth_mod.ensure_bootstrap_admin()
    assert dbmod.count_users() == 1


def test_bootstrap_admin_env_override(temp_db, monkeypatch):
    """ADMIN_EMAIL / ADMIN_PASSWORD 环境变量可覆盖内置默认账号。"""
    monkeypatch.setenv("CTA_ADMIN_EMAIL", "boss@corp.com")
    monkeypatch.setenv("CTA_ADMIN_PASSWORD", "super-secret-9")
    auth_mod.ensure_bootstrap_admin()
    user = dbmod.get_user_by_email("boss@corp.com")
    assert user is not None and user["role"] == "admin"
    assert auth_mod.verify_password("super-secret-9", user["password_hash"])


def test_register_never_creates_admin(temp_db, monkeypatch):
    """注册用户一律 role=user，即使注册的是管理员预留邮箱（撞 UNIQUE → 409）。"""
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    monkeypatch.setenv("CTA_ADMIN_EMAIL", "reserve@corp.com")
    auth_mod.ensure_bootstrap_admin()

    dbmod.create_invite_code("TESTCODE1", 10, None)
    a._RATE_BUCKETS.clear()

    async def _do():
        with pytest.raises(HTTPException) as exc:
            await a.register(a.RegisterRequest(
                email="reserve@corp.com", password="password123",
                confirm_password="password123", invite_code="TESTCODE1"))
        assert exc.value.status_code == 409
        return await a.register(a.RegisterRequest(
            email="normal@corp.com", password="password123",
            confirm_password="password123", invite_code="TESTCODE1"))

    resp_user = anyio.run(_do)
    assert resp_user["user"]["role"] == "user"  # register 直接返回 dict（无 response_model 序列化时）


# ── JWT / get_current_user ──


def test_jwt_roundtrip_and_tamper(temp_db):
    uid = dbmod.create_user("jwt@test.com", "h", role="admin")
    user = dbmod.get_user_by_id(uid)
    token = auth_mod.create_access_token(user)
    payload = auth_mod.decode_token(token)
    assert payload["sub"] == str(uid)
    assert payload["email"] == "jwt@test.com"
    assert payload["role"] == "admin"

    with pytest.raises(Exception):
        auth_mod.decode_token(token + "x")


def test_get_current_user_dependency(temp_db):
    uid = dbmod.create_user("dep@test.com", "h", role="user")
    user = dbmod.get_user_by_id(uid)
    token = auth_mod.create_access_token(user)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    current = auth_mod.get_current_user(creds)
    assert current == {"id": uid, "email": "dep@test.com", "role": "user"}
    assert auth_mod.user_key(current) == str(uid)
    assert auth_mod.profile_v2_key(current) == f"{uid}_v2"

    with pytest.raises(HTTPException) as exc:
        auth_mod.get_current_user(None)
    assert exc.value.status_code == 401

    bad = HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage.token.here")
    with pytest.raises(HTTPException) as exc:
        auth_mod.get_current_user(bad)
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        auth_mod.require_admin(current)
    assert exc.value.status_code == 403


# ── 会话归属 ──


def test_session_owner_isolation(temp_db):
    dbmod.touch_session("s1", "1")
    dbmod.touch_session("s2", "2")
    assert dbmod.get_session_owner("s1") == "1"
    assert dbmod.get_session_owner("s2") == "2"
    assert dbmod.get_session_owner("ghost") is None

    # 二次 touch（其他用户触发）不得覆盖归属
    dbmod.touch_session("s1", "2")
    assert dbmod.get_session_owner("s1") == "1"

    assert dbmod.get_session_ids_for_user("1") == {"s1"}
    assert dbmod.get_session_ids_for_user("2") == {"s2"}


# ── 提交按用户隔离 ──


def test_submissions_user_isolation(temp_db):
    pid, _reused = dbmod.save_problem({
        "title": f"T-{os.urandom(4).hex()}", "topic": "数组", "difficulty": "easy",
        "description": "d", "test_cases": [{"input_args": ["[1]"], "expected_output": "1"}],
    })
    dbmod.save_submission(pid, "code-a", "AC", [], session_id="s1", user_id="1")
    dbmod.save_submission(pid, "code-b", "WA", [], session_id="s2", user_id="2")

    subs_u1 = dbmod.get_submissions_by_problem(pid, user_id="1")
    subs_u2 = dbmod.get_submissions_by_problem(pid, user_id="2")
    assert [s["verdict"] for s in subs_u1] == ["AC"]
    assert [s["verdict"] for s in subs_u2] == ["WA"]

    assert dbmod.get_all_problem_verdicts(user_id="1") == {pid: "AC"}
    assert dbmod.get_all_problem_verdicts(user_id="2") == {pid: "WA"}
    # 不传 user_id → 全量（admin 视角）
    assert pid in dbmod.get_all_problem_verdicts()


def test_me_submissions_endpoint_isolation(temp_db):
    """GET /auth/me/submissions：个人中心「我的提交」，只返回 JWT 归属用户的数据。"""
    import os as _os

    from fastapi.testclient import TestClient

    from code_tutor_agent.api import auth as a
    from code_tutor_agent.api.main import app

    uid1 = dbmod.create_user("mine1@test.com", a.hash_password("password123"))
    uid2 = dbmod.create_user("mine2@test.com", a.hash_password("password123"))
    a._RATE_BUCKETS.clear()

    pid, _reused = dbmod.save_problem({
        "title": f"T-{os.urandom(4).hex()}", "topic": "数组", "difficulty": "easy",
        "description": "d", "test_cases": [{"input_args": ["[1]"], "expected_output": "1"}],
    })
    dbmod.save_submission(pid, "code-1", "AC", [], session_id="s1", user_id=str(uid1))
    dbmod.save_submission(pid, "code-2", "WA", [], session_id="s2", user_id=str(uid2))

    with TestClient(app) as c:
        def _h(email):
            tok = c.post("/auth/login", json={"email": email, "password": "password123"}).json()["token"]
            return {"Authorization": f"Bearer {tok}"}

        subs1 = c.get("/auth/me/submissions", headers=_h("mine1@test.com"))
        assert subs1.status_code == 200
        rows1 = subs1.json()["submissions"]
        assert [s["verdict"] for s in rows1] == ["AC"]
        assert all(s["problem_id"] == pid for s in rows1)

        subs2 = c.get("/auth/me/submissions", headers=_h("mine2@test.com"))
        assert [s["verdict"] for s in subs2.json()["submissions"]] == ["WA"]

        # 无 token → 401
        assert c.get("/auth/me/submissions").status_code == 401
        # 清理临时目录引用（保持 temp_db fixture 语义）
        del _os


# ── build_run_config user_id 贯穿 ──


def test_build_run_config_user_id():
    cfg = build_run_config("sid-x", user_id="42", run_name="t")
    assert cfg["configurable"]["user_id"] == "42"
    assert cfg["configurable"]["thread_id"] == "sid-x"
    assert cfg["metadata"]["user_id"] == "42"

    cfg2 = build_run_config("sid-x")  # 不传 → 不注入（legacy 行为不变）
    assert "user_id" not in cfg2["configurable"]
    assert "user_id" not in cfg2["metadata"]


# ── 邀请码 / 限流 / 改密 / 重置（防滥用改造，2026-09-06）──


def test_register_requires_invite_code(temp_db):
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    a._RATE_BUCKETS.clear()

    async def _do():
        with pytest.raises(HTTPException) as exc:
            await a.register(a.RegisterRequest(
                email="nocode@test.com", password="password123",
                confirm_password="password123", invite_code="BADCODE9"))
        return exc.value.status_code

    assert anyio.run(_do) == 400


def test_register_confirm_password_mismatch(temp_db):
    """两次密码不一致 → 400（后端硬校验，防前端绕过）。"""
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    a._RATE_BUCKETS.clear()
    dbmod.create_invite_code("MISMATCH", 10, None)

    async def _do():
        with pytest.raises(HTTPException) as exc:
            await a.register(a.RegisterRequest(
                email="mismatch@test.com", password="password123",
                confirm_password="password124", invite_code="MISMATCH"))
        return exc.value.status_code

    assert anyio.run(_do) == 400


def test_invite_code_quota_expiry_disable(temp_db):
    """额度用完 / 已过期 / 已停用的码都不可用；额度内可多次使用。"""
    from datetime import datetime as dt, timedelta as td

    # 额度=2
    assert dbmod.create_invite_code("QUOTA01", 2, None)
    assert dbmod.consume_invite_code("QUOTA01")  # DB 层不做大小写归一（归一在 register 边界）
    assert dbmod.consume_invite_code("QUOTA01")
    assert not dbmod.consume_invite_code("QUOTA01")  # 第 3 次超额

    # 已过期
    expired = (dt.now() - td(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert dbmod.create_invite_code("EXPIRE01", 10, expired)
    assert not dbmod.consume_invite_code("EXPIRE01")

    # 停用
    assert dbmod.create_invite_code("DEAD0001", 10, None)
    assert dbmod.consume_invite_code("DEAD0001")
    assert dbmod.set_invite_code_active("DEAD0001", False)
    assert not dbmod.consume_invite_code("DEAD0001")

    rows = dbmod.list_invite_codes()
    assert {r["code"] for r in rows} >= {"QUOTA01", "EXPIRE01", "DEAD0001"}


def test_register_rate_limit(temp_db, monkeypatch):
    """每 IP 每小时 5 次注册上限（direct 调用共桶）。"""
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    dbmod.create_invite_code("RATELIM1", 100, None)
    monkeypatch.setattr(a, "_RATE_BUCKETS", {})  # 独立桶避免污染其他测试

    async def _do():
        codes = []
        for i in range(6):
            try:
                await a.register(a.RegisterRequest(
                    email=f"rl{i}@test.com", password="password123",
                    confirm_password="password123", invite_code="RATELIM1"))
                codes.append(200)
            except HTTPException as exc:
                codes.append(exc.status_code)
        return codes

    codes = anyio.run(_do)
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


def test_change_password_flow(temp_db):
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    uid = dbmod.create_user("cp@test.com", a.hash_password("oldpassword1"))
    current = {"id": uid, "email": "cp@test.com", "role": "user"}

    async def _do():
        # 旧密码错 → 400
        with pytest.raises(HTTPException) as exc:
            await a.change_my_password(
                a.ChangePasswordRequest(old_password="wrong-pass-1", new_password="newpassword1"),
                current,
            )
        assert exc.value.status_code == 400
        # 正确 → 更新成功
        r = await a.change_my_password(
            a.ChangePasswordRequest(old_password="oldpassword1", new_password="newpassword1"),
            current,
        )
        assert r["ok"] is True

    anyio.run(_do)
    user = dbmod.get_user_by_id(uid)
    assert a.verify_password("newpassword1", user["password_hash"])
    assert not a.verify_password("oldpassword1", user["password_hash"])


def test_forgot_reset_no_hub(temp_db, monkeypatch):
    """未配置 mcp-hub：forgot 返回引导信息（不发码），reset 任何码都 400。"""
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    # 显式清空 hub 配置，隔离本地 .env（隐式依赖会假红）
    monkeypatch.delenv("MCP_HUB_TOKEN", raising=False)
    monkeypatch.delenv("SEARCH_MCP_TOKEN", raising=False)

    dbmod.create_user("nr@test.com", a.hash_password("whatever123"))

    async def _do():
        r = await a.forgot_password(a.ForgotPasswordRequest(email="nr@test.com"), None)
        assert r["delivered"] is False and "管理员" in r["message"]
        with pytest.raises(HTTPException) as exc:
            await a.reset_password(a.ResetPasswordRequest(
                email="nr@test.com", code="222222", new_password="newpassword1"), None)
        assert exc.value.status_code == 400

    anyio.run(_do)


def test_forgot_reset_with_hub(temp_db, monkeypatch):
    """配置 mcp-hub（mock 发信）：验证码送达 → 重置成功 → 旧密码失效 → 码一次性。"""
    import anyio

    from code_tutor_agent.api import auth as a
    from code_tutor_agent.api import email as email_svc

    uid = dbmod.create_user("br@test.com", a.hash_password("oldpassword1"))
    monkeypatch.setenv("MCP_HUB_TOKEN", "test-key")
    # 本文件 autouse fixture 强制 is_configured=False；本测试专测「hub 已配置」路径，显式打开
    monkeypatch.setattr(email_svc, "is_configured", lambda: True)
    monkeypatch.setattr(email_svc, "send_email", lambda *a2, **k: True)
    # 固定验证码为 222222（choice 恒返 '2'）
    monkeypatch.setattr(a.secrets, "choice", lambda s: "2")

    async def _do():
        r = await a.forgot_password(a.ForgotPasswordRequest(email="br@test.com"), None)
        assert r["delivered"] is True
        await a.reset_password(a.ResetPasswordRequest(
            email="br@test.com", code="222222", new_password="newpassword1"), None)
        # 同一码再用 → 已作废 → 400
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await a.reset_password(a.ResetPasswordRequest(
                email="br@test.com", code="222222", new_password="anotherpass1"), None)

    anyio.run(_do)
    user = dbmod.get_user_by_id(uid)
    assert a.verify_password("newpassword1", user["password_hash"])
    # 忘记密码接口防枚举：不存在的邮箱也 200 + 统一措辞
    async def _do2():
        return await a.forgot_password(a.ForgotPasswordRequest(email="ghost@nowhere.com"), None)
    r2 = anyio.run(_do2)
    assert r2["delivered"] is None and "几分钟内送达" in r2["message"]


def test_forgot_per_email_rate_limit(temp_db, monkeypatch):
    """per-email 限流：攻击者不断换 IP 针对同一邮箱请求，第 4 次须 429（堵邮件轰炸）。

    原限流只按 IP（forgot:<IP>），换 IP 即可绕开；本测试验证新增的
    forgot_email:<邮箱> 维度——同一目标邮箱每小时封顶 3 封，与来源 IP 无关。
    IP 维度限流保持默认（不关闭），仅靠「每请求换 IP」让 IP 桶无法累积，
    从而隔离证明是第 4 次被 per-email 维度拦截。
    """
    import anyio

    from code_tutor_agent.api import auth as a

    # 隔离本地 .env 的 hub 配置：确保 forgot 走「未配置」分支（不查 DB、不发信），
    # 把测试焦点完全收束到 per-email 限流本身（限流计数在 is_configured 判断之前已累积）。
    monkeypatch.delenv("MCP_HUB_TOKEN", raising=False)
    monkeypatch.delenv("SEARCH_MCP_TOKEN", raising=False)

    # 模拟攻击者每次请求换一个 IP（XFF 不同），使 IP 维度限流各计 1、永不触发
    ips = (f"203.0.113.{i}" for i in range(50))
    monkeypatch.setattr(a, "_client_ip", lambda req: next(ips))

    email = "bomb-victim@example.com"

    async def _attack():
        for _ in range(3):  # 前 3 次：不同 IP + 同一邮箱，均不应被限
            await a.forgot_password(a.ForgotPasswordRequest(email=email), None)
        with pytest.raises(HTTPException) as exc:  # 第 4 次：同一邮箱超限 → 429
            await a.forgot_password(a.ForgotPasswordRequest(email=email), None)
        assert exc.value.status_code == 429

    anyio.run(_attack)

    async def _other():  # 换邮箱不受影响：per-email 按目标邮箱隔离，不误伤其他用户
        await a.forgot_password(a.ForgotPasswordRequest(email="another-user@example.com"), None)
    anyio.run(_other)


def test_admin_users_and_invites_api(temp_db):
    """admin 用户列表 / 重置密码 / 邀请码生成-停用（TestClient 全链路）。"""
    import os as _os

    from fastapi.testclient import TestClient

    from code_tutor_agent.api import auth as a
    from code_tutor_agent.api.main import app

    admin_uid = dbmod.create_user("boss@test.com", a.hash_password("password123"), role="admin")
    victim_uid = dbmod.create_user("victim@test.com", a.hash_password("password123"))
    a._RATE_BUCKETS.clear()

    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"email": "boss@test.com", "password": "password123"}).json()["token"]
        ah = {"Authorization": f"Bearer {tok}"}

        users = c.get("/admin/users", headers=ah).json()["users"]
        assert {u["email"] for u in users} >= {"boss@test.com", "victim@test.com"}
        assert all("password_hash" not in u for u in users)

        # 普通用户访问 → 403
        victim_tok = c.post("/auth/login", json={"email": "victim@test.com", "password": "password123"}).json()["token"]
        assert c.get("/admin/users", headers={"Authorization": f"Bearer {victim_tok}"}).status_code == 403

        # 生成邀请码（额度 1）→ 用它注册一个新用户 → 第二次用同码超额
        inv = c.post("/admin/invites", headers=ah,
                     json={"max_uses": 1, "expires_days": 1, "note": "t"}).json()
        assert inv["ok"] and len(inv["code"]) == 8
        r = c.post("/auth/register", json={
            "email": "invited@test.com", "password": "password123",
            "confirm_password": "password123", "invite_code": inv["code"]})
        assert r.status_code == 200
        assert c.post("/auth/register", json={
            "email": "invited2@test.com", "password": "password123",
            "confirm_password": "password123", "invite_code": inv["code"]}).status_code == 400

        # admin 重置 victim 密码 → 临时密码可登录
        reset = c.post(f"/admin/users/{victim_uid}/reset-password", headers=ah).json()
        assert reset["ok"] and reset["temp_password"]
        c2 = TestClient(app)
        login_r = c2.post("/auth/login", json={
            "email": "victim@test.com", "password": reset["temp_password"]})
        assert login_r.status_code == 200

        # 停用码立即失效
        inv2 = c.post("/admin/invites", headers=ah, json={"max_uses": 5, "expires_days": 0}).json()
        assert c.post(f"/admin/invites/{inv2['code']}/disable", headers=ah).status_code == 200
        assert c.post("/auth/register", json={
            "email": "after@test.com", "password": "password123",
            "confirm_password": "password123", "invite_code": inv2["code"]}).status_code == 400
        # 清理临时目录引用（保持 temp_db fixture 语义）
        del _os


# ── 公开邀请码（注册页免填，2026-09-08）──


def test_public_invite_code_db_layer(temp_db):
    """is_public 标记驱动 get_public_invite_code：未标记不暴露、标记后返回、可来回切换。"""
    assert dbmod.get_public_invite_code() is None  # 开局无公开码

    # 普通（非公开）码不得被公开接口暴露
    dbmod.create_invite_code("PRIV001", 10, None)
    assert dbmod.get_public_invite_code() is None

    # 设为公开
    dbmod.create_invite_code("PUB001", 10, None, is_public=True)
    assert dbmod.get_public_invite_code() == "PUB001"

    # 取消公开
    assert dbmod.set_invite_code_public("PUB001", False)
    assert dbmod.get_public_invite_code() is None

    # 再设回
    assert dbmod.set_invite_code_public("PUB001", True)
    assert dbmod.get_public_invite_code() == "PUB001"

    # list_invite_codes 现在带 is_public 字段
    rows = {r["code"]: r["is_public"] for r in dbmod.list_invite_codes()}
    assert rows.get("PUB001") == 1 and rows.get("PRIV001") == 0


def test_public_invite_picks_most_recent_valid(temp_db):
    """多个有效公开码并存时，取创建时间最新的那个（admin 轮换即生效）。"""
    dbmod.create_invite_code("OLDPUB", 10, None, is_public=True)
    dbmod.create_invite_code("NEWPUB", 10, None, is_public=True)
    assert dbmod.get_public_invite_code() == "NEWPUB"


def test_public_invite_excludes_invalid(temp_db):
    """过期 / 额满的公开码不返回；仍有有效公开码时返回它。"""
    from datetime import datetime as dt, timedelta as td

    # 过期公开码
    expired = (dt.now() - td(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    dbmod.create_invite_code("EXPPIB", 10, expired, is_public=True)
    assert dbmod.get_public_invite_code() is None

    # 额满公开码
    dbmod.create_invite_code("FULLPUB", 1, None, is_public=True)
    assert dbmod.consume_invite_code("FULLPUB")
    assert dbmod.get_public_invite_code() is None

    # 兜底有效公开码
    dbmod.create_invite_code("GOODPUB", 10, None, is_public=True)
    assert dbmod.get_public_invite_code() == "GOODPUB"


def test_register_with_public_code(temp_db):
    """公开码仍可被正常消费注册（is_public 不影响注册逻辑，只是免手填）。"""
    import anyio
    from fastapi import HTTPException

    from code_tutor_agent.api import auth as a

    a._RATE_BUCKETS.clear()
    dbmod.create_invite_code("PUBREG01", 10, None, is_public=True)

    async def _do():
        return await a.register(a.RegisterRequest(
            email="pubreg@test.com", password="password123",
            confirm_password="password123", invite_code="PUBREG01"))

    resp = anyio.run(_do)
    assert resp["user"]["role"] == "user"
    assert dbmod.get_public_invite_code() == "PUBREG01"  # 额度 10，用掉 1 仍有效


def test_public_invite_endpoint_unauthenticated(temp_db):
    """GET /auth/public-invite 免登录：无公开码返回 {enabled:false}，有则吐码。"""
    import os as _os

    from fastapi.testclient import TestClient

    from code_tutor_agent.api import auth as a
    from code_tutor_agent.api.main import app

    with TestClient(app) as c:
        # 无公开码 → 200 + {enabled:false}
        r = c.get("/auth/public-invite")
        assert r.status_code == 200
        assert r.json() == {"enabled": False, "email_verification": False}

        # 直写公开码（免 admin 登录）
        dbmod.create_invite_code("PUBLIC99", 5, None, is_public=True)
        r2 = c.get("/auth/public-invite")
        assert r2.status_code == 200
        body = r2.json()
        assert body["enabled"] is True
        assert body["invite_code"] == "PUBLIC99"
        del _os


def test_admin_toggle_public_invite_endpoint(temp_db):
    """admin 生成即标记公开 / 列表展示 is_public / 取消公开；普通用户操作 → 403。"""
    import os as _os

    from fastapi.testclient import TestClient

    from code_tutor_agent.api import auth as a
    from code_tutor_agent.api.main import app

    admin_uid = dbmod.create_user("boss2@test.com", a.hash_password("password123"), role="admin")
    dbmod.create_user("user2@test.com", a.hash_password("password123"))
    a._RATE_BUCKETS.clear()
    assert admin_uid

    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"email": "boss2@test.com", "password": "password123"}).json()["token"]
        ah = {"Authorization": f"Bearer {tok}"}
        victim_tok = c.post("/auth/login", json={"email": "user2@test.com", "password": "password123"}).json()["token"]
        vh = {"Authorization": f"Bearer {victim_tok}"}

        # 生成即标记公开
        inv = c.post("/admin/invites", headers=ah,
                     json={"max_uses": 3, "expires_days": 0, "is_public": True}).json()
        assert inv["ok"] and inv["is_public"] is True

        # 列表带 is_public 字段
        rows = c.get("/admin/invites", headers=ah).json()["invites"]
        pub = next(r for r in rows if r["code"] == inv["code"])
        assert pub["is_public"] == 1

        # 注册页能拉到
        assert c.get("/auth/public-invite").json()["invite_code"] == inv["code"]

        # 普通用户取消公开 → 403
        assert c.post(f"/admin/invites/{inv['code']}/public", headers=vh,
                      json={"public": False}).status_code == 403

        # admin 取消公开 → 注册页不再暴露
        assert c.post(f"/admin/invites/{inv['code']}/public", headers=ah,
                      json={"public": False}).status_code == 200
        assert c.get("/auth/public-invite").json()["enabled"] is False
        del _os