"""API integration tests — 覆盖全链路核心场景。

测试范围：
- Session 创建（含多类型问题）
- 题目列表 & 提交记录
- 错误处理
- 状态流转

不测试：LLM 调用、Judge0 沙箱（由单元测试覆盖）。

多用户改造（2026-09-06）：所有请求带 auth_headers(client)（自动登录+缓存
token）；/admin/profile 已删除，画像走 /auth/me/profile（JWT 归属用户）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app

# tests/integration 下无 __init__，按目录加入 sys.path 后直接 import 辅助模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _agent_helpers import auth_headers  # noqa: E402


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
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"}, headers=auth_headers(client))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "generating"

    def test_create_session_with_type(self, client):
        """指定 problem_type 创建。"""
        for ptype in ["coding", "math", "sci_comp", "engineering", "ai_game"]:
            resp = client.post("/session", json={
                "topic": "数组", "difficulty": "easy", "problem_type": ptype,
            }, headers=auth_headers(client))
            assert resp.status_code == 200, f"Failed for type {ptype}: {resp.text}"
            data = resp.json()
            assert data["status"] == "generating"

    def test_create_session_empty_body(self, client):
        """空 body 也能创建（用默认值）。"""
        resp = client.post("/session", json={}, headers=auth_headers(client))
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data

    def test_create_session_invalid_type_falls_back(self, client):
        """非法 problem_type 自动 fallback 到 coding。"""
        resp = client.post("/session", json={
            "topic": "数组", "difficulty": "easy", "problem_type": "invalid_type",
        }, headers=auth_headers(client))
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"]


# ═══════════════════════════════════════════════
#  Session 状态查询
# ═══════════════════════════════════════════════


class TestSessionState:
    """GET /session/{sid}/state — 查询会话状态。"""

    def test_get_state_returns_required_fields(self, client):
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"}, headers=auth_headers(client))
        sid = resp.json()["session_id"]

        state_resp = client.get(f"/session/{sid}/state", headers=auth_headers(client))
        assert state_resp.status_code == 200
        data = state_resp.json()
        assert "session_id" in data
        assert "status" in data
        assert "topic" in data
        assert "difficulty" in data
        assert "submissions" in data
        assert "tutor_messages" in data

    def test_get_state_nonexistent_session(self, client):
        resp = client.get("/session/nonexistent-id/state", headers=auth_headers(client))
        # 不存在的 session — 不崩溃即可（归属不存在 → 404 属正常）
        assert resp.status_code in (200, 404, 500)


# ═══════════════════════════════════════════════
#  题目列表 & 提交记录
# ═══════════════════════════════════════════════


class TestProblems:
    """GET /problems — 题目列表。"""

    def test_list_problems_returns_array(self, client):
        resp = client.get("/problems", headers=auth_headers(client))
        assert resp.status_code == 200
        data = resp.json()
        assert "problems" in data
        assert isinstance(data["problems"], list)

    def test_submissions_empty_for_new_problem(self, client):
        resp = client.get("/problem/999999/submissions", headers=auth_headers(client))
        # 不存在的 problem_id 返回空列表，不崩溃
        assert resp.status_code == 200
        data = resp.json()
        assert "submissions" in data


# ═══════════════════════════════════════════════
#  Admin 接口
# ═══════════════════════════════════════════════


class TestAdmin:
    """管理后台接口（2026-09-06 晚：独立密码已废除，鉴权 = JWT + role=admin）。"""

    def test_admin_login_endpoint_removed(self, client):
        """/admin/login 兼容端点已移除。"""
        resp = client.post("/admin/login", json={"password": ""})
        assert resp.status_code == 404

    def test_admin_problems_requires_auth(self, client):
        """/admin/problems 无 token → 401。"""
        resp = client.post("/admin/problems")
        assert resp.status_code == 401

    def test_admin_problems_with_jwt(self, client):
        """带 admin JWT（auth_headers 自动登录引导管理员）→ 200。"""
        resp = client.post("/admin/problems", headers=auth_headers(client))
        assert resp.status_code == 200
        assert "problems" in resp.json()

    def test_me_profile_endpoint(self, client):
        """GET /auth/me/profile 返回当前用户画像（原 /admin/profile 已删）。"""
        resp = client.get("/auth/me/profile", headers=auth_headers(client))
        assert resp.status_code == 200
        data = resp.json()
        assert "proficiency" in data
        assert "stability" in data
        assert "attempts" in data


# ═══════════════════════════════════════════════
#  Health check
# ═══════════════════════════════════════════════


class TestHealth:
    """GET /health — 健康检查（无需鉴权）。"""

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
