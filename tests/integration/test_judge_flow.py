"""集成测试：判题流程 — 提交代码 → 判题 → 画像更新。

覆盖场景：
- 创建 session 的状态
- 画像 v2 接口结构
- 画像 v1 兼容性
- 并发 session
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestJudgeFlow:
    """判题流程集成测试。"""

    def test_session_creation_sets_correct_status(self, client):
        """创建 session 后状态为 generating。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "generating"

    def test_session_has_unique_id(self, client):
        """两次创建 session 得到不同 id。"""
        r1 = client.post("/session", json={"topic": "数组", "difficulty": "easy"}).json()
        r2 = client.post("/session", json={"topic": "数组", "difficulty": "easy"}).json()
        assert r1["session_id"] != r2["session_id"]

    def test_state_after_session_creation(self, client):
        """创建 session 后 state 有基本字段。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]
        resp = client.get(f"/session/{sid}/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid
        assert "topic" in data
        assert "difficulty" in data
        assert "submissions" in data
        assert "tutor_messages" in data

    def test_profile_v2_endpoint_returns_valid_json(self, client):
        """GET /admin/profile/v2 返回有效 JSON。"""
        resp = client.get("/admin/profile/v2")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_profile_v2_has_all_required_fields(self, client):
        """新画像应包含所有必需字段。"""
        resp = client.get("/admin/profile/v2")
        data = resp.json()
        for field in ["prof", "prof_elo_raw", "stab", "forget", "errors", "attempts", "meta"]:
            assert field in data, f"Missing: {field}"

    def test_profile_v2_errors_structure(self, client):
        """errors 字段应有 _global 和 per_tag。"""
        resp = client.get("/admin/profile/v2")
        data = resp.json()
        assert "_global" in data["errors"]
        assert "per_tag" in data["errors"]

    def test_profile_v2_meta_schema_version(self, client):
        """meta.schema_version 应为 'mvp@1'。"""
        resp = client.get("/admin/profile/v2")
        data = resp.json()
        assert data["meta"].get("schema_version") == "mvp@1"

    def test_profile_v1_still_works(self, client):
        """GET /admin/profile 旧接口仍可用。"""
        resp = client.get("/admin/profile")
        assert resp.status_code == 200
        data = resp.json()
        for field in ["proficiency", "stability", "forget_days", "common_errors", "attempts"]:
            assert field in data, f"Missing v1 field: {field}"

    def test_profile_v1_values_are_reasonable(self, client):
        """旧画像数值范围合理。"""
        resp = client.get("/admin/profile")
        data = resp.json()
        assert 0 <= data["proficiency"] <= 1.0
        assert 0 <= data["stability"] <= 1.0
        assert data["attempts"] >= 0
        assert data["forget_days"] >= 0
        assert isinstance(data["common_errors"], list)

    def test_concurrent_sessions_unique_ids(self, client):
        """同时创建多个 session 不冲突。"""
        sids = [client.post("/session", json={"topic": "数组", "difficulty": "easy"}).json()["session_id"]
                for _ in range(3)]
        assert len(set(sids)) == 3

    def test_invalid_json_body_returns_422(self, client):
        """非法 JSON body 应返回 422。"""
        resp = client.post("/session", content=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 422

    def test_invalid_topic_creates_session(self, client):
        """非法 topic 不崩溃。"""
        resp = client.post("/session", json={"topic": "不存在的话题", "difficulty": "easy"})
        assert resp.status_code == 200
        assert "session_id" in resp.json()

    def test_empty_body_creates_session(self, client):
        """空 body 也能创建 session。"""
        resp = client.post("/session", json={})
        assert resp.status_code == 200
        assert "session_id" in resp.json()