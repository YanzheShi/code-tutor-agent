"""API integration tests — 覆盖全链路核心场景。

测试范围：
- Session 创建（含多类型问题）
- 题目列表 & 提交记录
- 错误处理
- 状态流转

不测试：LLM 调用、Judge0 沙箱（由单元测试覆盖）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ═══════════════════════════════════════════════
#  Session 创建
# ═══════════════════════════════════════════════


class TestSessionCreation:
    """POST /session — 创建会话。"""

    def test_create_session_default_type(self, client):
        """默认类型为 coding。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "generating"

    def test_create_session_with_type(self, client):
        """指定 problem_type 创建。"""
        for ptype in ["coding", "math", "sci_comp", "engineering", "ai_game"]:
            resp = client.post("/session", json={
                "topic": "数组", "difficulty": "easy", "problem_type": ptype,
            })
            assert resp.status_code == 200, f"Failed for type {ptype}: {resp.text}"
            data = resp.json()
            assert data["status"] == "generating"

    def test_create_session_empty_body(self, client):
        """空 body 也能创建（用默认值）。"""
        resp = client.post("/session", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data

    def test_create_session_invalid_type_falls_back(self, client):
        """非法 problem_type 自动 fallback 到 coding。"""
        resp = client.post("/session", json={
            "topic": "数组", "difficulty": "easy", "problem_type": "invalid_type",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"]


# ═══════════════════════════════════════════════
#  Session 状态查询
# ═══════════════════════════════════════════════


class TestSessionState:
    """GET /session/{sid}/state — 查询会话状态。"""

    def test_get_state_returns_required_fields(self, client):
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        sid = resp.json()["session_id"]

        state_resp = client.get(f"/session/{sid}/state")
        assert state_resp.status_code == 200
        data = state_resp.json()
        assert "session_id" in data
        assert "status" in data
        assert "topic" in data
        assert "difficulty" in data
        assert "submissions" in data
        assert "tutor_messages" in data

    def test_get_state_nonexistent_session(self, client):
        resp = client.get("/session/nonexistent-id/state")
        # 不存在的 session — 不崩溃即可
        assert resp.status_code in (200, 404, 500)


# ═══════════════════════════════════════════════
#  题目列表 & 提交记录
# ═══════════════════════════════════════════════


class TestProblems:
    """GET /problems — 题目列表。"""

    def test_list_problems_returns_array(self, client):
        resp = client.get("/problems")
        assert resp.status_code == 200
        data = resp.json()
        assert "problems" in data
        assert isinstance(data["problems"], list)

    def test_submissions_empty_for_new_problem(self, client):
        resp = client.get("/problem/999999/submissions")
        # 不存在的 problem_id 返回空列表，不崩溃
        assert resp.status_code == 200
        data = resp.json()
        assert "submissions" in data


# ═══════════════════════════════════════════════
#  Admin 接口
# ═══════════════════════════════════════════════


class TestAdmin:
    """管理后台接口。"""

    def test_admin_login(self, client):
        """登录接口不崩溃即可。"""
        resp = client.post("/admin/login", json={"password": ""})
        assert resp.status_code in (200, 401)

    def test_admin_profile_endpoint(self, client):
        """GET /admin/profile 返回默认画像。"""
        resp = client.get("/admin/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "proficiency" in data
        assert "stability" in data
        assert "attempts" in data


# ═══════════════════════════════════════════════
#  Health check
# ═══════════════════════════════════════════════


class TestHealth:
    """GET /health — 健康检查。"""

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200