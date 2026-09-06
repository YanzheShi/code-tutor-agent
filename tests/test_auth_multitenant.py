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
    assert "UNIQUE" in str(exc.value)


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

    async def _do():
        with pytest.raises(HTTPException) as exc:
            await a.register(a.RegisterRequest(email="reserve@corp.com", password="password123"))
        assert exc.value.status_code == 409
        return await a.register(a.RegisterRequest(email="normal@corp.com", password="password123"))

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


# ── build_run_config user_id 贯穿 ──


def test_build_run_config_user_id():
    cfg = build_run_config("sid-x", user_id="42", run_name="t")
    assert cfg["configurable"]["user_id"] == "42"
    assert cfg["configurable"]["thread_id"] == "sid-x"
    assert cfg["metadata"]["user_id"] == "42"

    cfg2 = build_run_config("sid-x")  # 不传 → 不注入（legacy 行为不变）
    assert "user_id" not in cfg2["configurable"]
    assert "user_id" not in cfg2["metadata"]
