"""Test: DB operations coverage — optimal_solution update, error states."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


class TestUpdateProblemOptimalSolution:
    """Coverage for update_problem_optimal_solution()."""

    def _setup(self):
        from code_tutor_agent.db import database as dbmod
        orig = dbmod.DB_PATH
        tpath = os.path.join(tempfile.gettempdir(), f"test_opt_{os.getpid()}.db")
        if os.path.exists(tpath):
            os.remove(tpath)
        dbmod.DB_PATH = tpath
        return dbmod, orig, tpath

    def _teardown(self, dbmod, orig, tpath):
        dbmod.DB_PATH = orig
        if os.path.exists(tpath):
            os.remove(tpath)

    def test_update_populates_field(self):
        dbmod, orig, tpath = self._setup()
        try:
            pid, _ = dbmod.save_problem({
                "title": "test_opt_update",
                "topic": "数组", "difficulty": "easy",
                "description": "test",
                "test_cases": [], "brute_solution": "",
            })
            assert pid > 0
            dbmod.update_problem_optimal_solution(pid, "class Solution:\n    def test(self): pass\n")
            r = dbmod.get_problem_by_id(pid)
            assert r is not None
            assert "class Solution:" in r.get("optimal_solution", "")
            assert "def test" in r.get("optimal_solution", "")
        finally:
            self._teardown(dbmod, orig, tpath)

    def test_update_overwrites_existing(self):
        dbmod, orig, tpath = self._setup()
        try:
            pid, _ = dbmod.save_problem({
                "title": "test_opt_overwrite",
                "topic": "数组", "difficulty": "easy",
                "description": "test",
                "optimal_solution": "old_code",
                "test_cases": [], "brute_solution": "",
            })
            dbmod.update_problem_optimal_solution(pid, "new_code")
            r = dbmod.get_problem_by_id(pid)
            assert r.get("optimal_solution") == "new_code"
        finally:
            self._teardown(dbmod, orig, tpath)

    def test_update_nonexistent_problem_does_not_crash(self):
        from code_tutor_agent.db import database as dbmod
        orig, tpath = dbmod.DB_PATH, os.path.join(tempfile.gettempdir(), f"test_opt_nope_{os.getpid()}.db")
        dbmod.DB_PATH = tpath
        try:
            dbmod.init_db()
            dbmod.update_problem_optimal_solution(99999, "code")
        finally:
            dbmod.DB_PATH = orig
            if os.path.exists(tpath):
                os.remove(tpath)



def _auth_headers(client) -> dict:
    """注册一个测试用户并返回带 Bearer 的请求头（多用户改造后端点需要 JWT）。"""
    import uuid as _uuid
    from unittest.mock import patch

    from code_tutor_agent.api import email as _email
    from code_tutor_agent.db import database as _db
    _db.create_invite_code("DBERRCODE", 100000, None)  # 邀请码注册制
    email = f"dberr-{_uuid.uuid4().hex[:8]}@test.com"
    # 2026-09-10 注册接入邮箱验证码（is_configured 可用时强制）；本套件只测 DB 错误态，
    # 固定走「邮件未配置 → 仅邀请码」降级路径，避免依赖开发者 .env 是否配了 MCP_HUB。
    with patch.object(_email, "is_configured", return_value=False):
        r = client.post("/auth/register", json={"email": email, "password": "password123",
                                                "confirm_password": "password123",
                                                "invite_code": "DBERRCODE"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}

class TestSessionErrorStates:
    """Error handling in session endpoints."""

    @pytest.fixture(autouse=True)
    def _client(self):
        from code_tutor_agent.api.main import app
        with TestClient(app) as c:
            self.client = c
            self.headers = _auth_headers(c)

    def test_get_state_nonexistent_session(self):
        resp = self.client.get(
            "/session/00000000-0000-0000-0000-000000000000/state", headers=self.headers)
        # 生产契约（改造前已如此）：不存在的会话 → 404，前端据此区分「生成中」与「无效会话」
        assert resp.status_code == 404

    def test_submit_nonexistent_session_returns_error(self):
        """POST /session/{id}/submit for non-existent session should error."""
        import contextlib
        with contextlib.suppress(Exception):
            resp = self.client.post(
            "/session/00000000-0000-0000-0000-000000000000/submit", headers=self.headers,
                json={"code": "print(1)", "language": "python"},
            )
            assert resp.status_code >= 400

    def test_get_reference_nonexistent_session_returns_error(self):
        resp = self.client.get(
            "/session/00000000-0000-0000-0000-000000000000/reference", headers=self.headers)
        assert resp.status_code >= 400

    def test_run_nonexistent_session_returns_error(self):
        resp = self.client.post(
            "/session/00000000-0000-0000-0000-000000000000/run", headers=self.headers,
            json={"code": "print(1)", "language": "python"},
        )
        assert resp.status_code >= 400

    def test_chat_nonexistent_session_returns_error(self):
        import contextlib
        with contextlib.suppress(Exception):
            resp = self.client.post(
            "/session/00000000-0000-0000-0000-000000000000/chat/stream", headers=self.headers,
                json={"message": "hello"},
            )
            assert resp.status_code >= 400


class TestSerializationEdgeCases:
    """Serialize_state edge cases."""

    @pytest.fixture(autouse=True)
    def _setup_api(self):
        from code_tutor_agent.api.main import app
        with TestClient(app) as c:
            self.client = c
            self.headers = _auth_headers(c)

    def test_create_session_returns_generating(self):
        resp = self.client.post(
            "/session", headers=self.headers, json={"topic": "数组", "difficulty": "easy", "mode": "practice"})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "generating"

    def test_create_session_empty_body(self):
        resp = self.client.post(
            "/session", headers=self.headers, json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data

    def test_problems_endpoint(self):
        resp = self.client.get(
            "/problems", headers=self.headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)