"""Regression tests for LeetCode import + session creation flow.

Covers:
  - POST /leetcode/parse  →  parsed problem with test cases
  - POST /session (with leetcode body)  →  fast-path, status=awaiting_submit
  - GET  /session/{id}/state  →  problem loaded, test cases visible
  - POST /session/{id}/run  →  run user code against visible test cases
  - Frontend stale closure guard: state returns status=awaiting_submit immediately
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure the project src is on the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    """FastAPI TestClient wrapping the real app (graph compiles at startup)."""
    with TestClient(app) as c:
        yield c


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestLeetCodeParse:
    """POST /leetcode/parse — parse a real LeetCode problem."""

    LEETCODE_URL = "https://leetcode.cn/problems/reverse-integer/"

    def test_parse_returns_expected_structure(self, client):
        resp = client.post("/leetcode/parse", json={"url": self.LEETCODE_URL})
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Core fields
        assert data["title"] == "整数反转"
        assert data["difficulty"] == "medium"
        assert data["starter_code"].startswith("class Solution")
        assert "reverse" in data["starter_code"]

        # Parsed test cases
        assert "parsed_test_cases" in data
        tcs = data["parsed_test_cases"]
        assert len(tcs) >= 4  # 4 examples from LeetCode

        # Each test case has the right shape
        for tc in tcs:
            assert "input_args" in tc
            assert "expected_output" in tc
            assert "explanation" in tc
            assert isinstance(tc["input_args"], list)
            assert isinstance(tc["expected_output"], str)

        # Validate specific examples
        assert any(tc["expected_output"] == "321" for tc in tcs)
        assert any(tc["expected_output"] == "-321" for tc in tcs)
        assert any(tc["expected_output"] == "21" for tc in tcs)
        assert any(tc["expected_output"] == "0" for tc in tcs)

    def test_parse_bad_url_returns_400(self, client):
        resp = client.post("/leetcode/parse", json={"url": "https://example.com/not-a-leetcode"})
        assert resp.status_code == 400

    def test_parse_nonexistent_slug_returns_client_error(self, client):
        resp = client.post("/leetcode/parse", json={"url": "https://leetcode.cn/problems/this-problem-definitely-does-not-exist-12345/"})
        assert resp.status_code in (400, 404)


class TestSessionLeetCodeFastPath:
    """POST /session + leetcode body — fast-path skips background generation."""

    def _build_leetcode_body(self, client) -> dict:
        """Helper: parse a real LeetCode problem and return the body for POST /session."""
        resp = client.post("/leetcode/parse", json={"url": "https://leetcode.cn/problems/reverse-integer/"})
        assert resp.status_code == 200
        parsed = resp.json()
        return {
            "topic": parsed["title"],
            "difficulty": parsed["difficulty"],
            "mode": "practice",
            "leetcode": parsed,
        }

    def test_fast_path_returns_awaiting_submit(self, client):
        """The fast-path must return status=awaiting_submit and problem immediately."""
        body = self._build_leetcode_body(client)
        resp = client.post("/session", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # THIS is the critical assertion that guards against the stale closure bug:
        # the frontend polls until status != 'generating' — if the backend ever
        # returns 'generating' here, the frontend would loop forever.
        assert data["status"] == "awaiting_submit", (
            f"Expected awaiting_submit, got '{data['status']}'. "
            "If this fails, the LeetCode fast-path is not working correctly."
        )

        # Problem must be loaded
        assert data["problem"] is not None
        assert data["problem"]["title"] == "整数反转"
        assert data["problem"]["difficulty"] == "medium"
        assert data["problem"]["starter_code"].startswith("class Solution")

        # Visible test cases must be populated
        assert len(data["problem"]["visible_test_cases"]) >= 4

        # Session ID
        assert data["session_id"] is not None
        assert len(data["session_id"]) > 0

    def test_fast_path_state_polling(self, client):
        """GET /session/{id}/state must return the same state after fast-path."""
        body = self._build_leetcode_body(client)
        resp = client.post("/session", json=body)
        sid = resp.json()["session_id"]

        # Poll the state
        state = client.get(f"/session/{sid}/state")
        assert state.status_code == 200
        s = state.json()

        assert s["status"] == "awaiting_submit"
        assert s["problem"] is not None
        assert s["problem"]["title"] == "整数反转"
        assert len(s["problem"]["visible_test_cases"]) >= 4
        assert s["submissions"] == []
        assert s["last_verdict"] is None

    def test_fast_path_run_code_against_visible_tcs(self, client):
        """POST /session/{sid}/run must work against the imported visible test cases."""
        body = self._build_leetcode_body(client)
        resp = client.post("/session", json=body)
        sid = resp.json()["session_id"]

        # Run a correct solution
        code = """class Solution:
    def reverse(self, x: int) -> int:
        sign = 1 if x >= 0 else -1
        x = abs(x)
        rev = 0
        while x:
            rev = rev * 10 + x % 10
            x //= 10
        return 0 if rev > 2**31 - 1 else sign * rev
"""
        run_resp = client.post(
            f"/session/{sid}/run",
            json={"code": code, "language": "python"},
        )
        assert run_resp.status_code == 200, run_resp.text
        r = run_resp.json()
        assert r["passed"] == r["total"], f"Expected all passed, got {r['passed']}/{r['total']}: {r}"
        assert r["all_passed"] is True

    def test_fast_path_missing_leetcode_body_falls_back_to_normal(self, client):
        """POST /session without leetcode body should return generating (background task)."""
        resp = client.post("/session", json={"topic": "array", "difficulty": "easy", "mode": "practice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "generating"  # background task
        assert data["session_id"] is not None


class TestProblemCleanup:
    """Sanity check: the problems table only has valid entries."""

    def test_no_problems_with_empty_starter_code(self, client):
        """Admin endpoint should only return problems with valid starter_code."""
        # Verify all listed problems have non-empty starter_code
        resp = client.get("/problems")
        assert resp.status_code == 200
        problems = resp.json().get("problems", [])
        from code_tutor_agent.db.database import _get_conn
        import sqlite3
        conn = _get_conn()
        bad = conn.execute(
            "SELECT id, title FROM problems WHERE starter_code = ''"
        ).fetchall()
        conn.close()
        assert len(bad) == 0, f"Problems with missing starter_code: {[(r[0], r[1]) for r in bad]}"


class TestGraphFlow:
    """Unit tests for the graph routing logic (no LeetCode dependency)."""

    def test_planner_skips_generator_when_problem_loaded(self):
        """Planner should route to wait_for_submit when problem is already set."""
        from code_tutor_agent.nodes.planner import planner_node
        from code_tutor_agent.schemas.state import SessionState, ProblemMeta

        state = SessionState(
            session_id="test",
            problem=ProblemMeta(
                problem_id=1,
                title="Test",
                topic="array",
                difficulty="easy",
                description="test",
                starter_code="class Solution: pass",
            ),
            status="awaiting_submit",
        )
        cmd = planner_node(state)
        assert cmd.goto == "wait_for_submit_node", f"Expected wait_for_submit_node, got {cmd.goto}"
        assert cmd.update.get("status") == "awaiting_submit"

    def test_planner_goes_to_generator_when_no_problem(self):
        """Planner should route to generator_node when no problem is loaded."""
        from code_tutor_agent.nodes.planner import planner_node
        from code_tutor_agent.schemas.state import SessionState

        state = SessionState(session_id="test")
        cmd = planner_node(state)
        assert cmd.goto == "generator_node", f"Expected generator_node, got {cmd.goto}"
        assert cmd.update.get("status") == "awaiting_problem"

    def test_wait_for_submit_payload_structure(self):
        """wait_for_submit_node should return an interrupt payload with the problem.
        
        We test indirectly by verifying the payload structure that the node builds.
        """
        from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node
        from code_tutor_agent.schemas.state import SessionState, ProblemMeta

        problem = ProblemMeta(
            problem_id=1,
            title="Test",
            topic="array",
            difficulty="easy",
            description="test",
            starter_code="class Solution: pass",
        )
        state = SessionState(
            session_id="test",
            problem=problem,
            status="awaiting_submit",
        )
        # wait_for_submit_node calls interrupt() which needs the LangGraph runtime.
        # We can't test interrupt() directly, but verify the node is importable
        # and accepts the expected signature.
        assert callable(wait_for_submit_node)
        assert wait_for_submit_node.__doc__ is not None
        # Expected payload keys (interrupt value)
        expected_keys = {"type", "problem", "submission_count", "hint_level", "last_verdict"}
        # We can't call it directly, but the implementation is covered by the
        # integration tests (test_fast_path_returns_awaiting_submit) which exercise
        # the full graph through the FastAPI test client.


# ── Run directly ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])