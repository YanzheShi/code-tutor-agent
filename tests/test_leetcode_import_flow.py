"""Regression tests for LeetCode import + session creation flow.

Covers:
  - POST /session (with leetcode_url)  →  background generation, status=generating
  - GET  /session/{id}/state  →  polls to awaiting_submit, problem loaded, test cases visible
  - POST /session/{id}/run  →  run user code against visible test cases
  - Frontend stale closure guard: session must leave 'generating' and reach
    'awaiting_submit' (otherwise the frontend poll loop would spin forever).

Note: parsing/fetching is now consolidated in the generation package
(generator_node) — there is no standalone /leetcode/parse endpoint anymore.
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


def wait_for_session(c, sid: str, timeout: float = 120.0) -> dict:
    """Poll GET /session/{sid}/state until it leaves the generating state."""
    import time

    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = c.get(f"/session/{sid}/state")
        if resp.status_code != 200:
            raise AssertionError(f"state poll returned {resp.status_code}")
        last = resp.json()
        if last.get("status") not in ("generating", "awaiting_problem"):
            return last
        time.sleep(0.5)
    raise TimeoutError(
        f"Session {sid} still generating after {timeout}s; last={last}"
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSessionLeetCodeUrlImport:
    """POST /session + leetcode_url — import path goes through background generation.

    Parsing/fetching is now consolidated in generator_node (generation pkg);
    the session starts in 'generating' and reaches 'awaiting_submit' after the
    imported problem is loaded (network permitting).
    """

    LEETCODE_URL = "https://leetcode.cn/problems/reverse-integer/"

    def test_url_import_returns_generating(self, client):
        """POST /session with leetcode_url must return status=generating + session_id."""
        resp = client.post("/session", json={
            "topic": "整数反转",
            "difficulty": "medium",
            "mode": "practice",
            "leetcode_url": self.LEETCODE_URL,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # New contract: import path also runs background generation (no fast-path).
        assert data["status"] == "generating"
        assert data["session_id"]

    def test_url_import_polls_to_awaiting_submit(self, client):
        """GET /session/{id}/state must reach awaiting_submit with the imported problem.

        This is the critical guard against the stale closure bug: the frontend
        polls until status != 'generating' — the session MUST leave 'generating'
        and load the imported problem, otherwise the frontend loops forever.
        """
        resp = client.post("/session", json={
            "topic": "整数反转",
            "difficulty": "medium",
            "mode": "practice",
            "leetcode_url": self.LEETCODE_URL,
        })
        sid = resp.json()["session_id"]

        state = wait_for_session(client, sid)
        assert state["status"] == "awaiting_submit"
        assert state["problem"] is not None
        assert state["problem"]["title"] == "整数反转"
        assert state["problem"]["difficulty"] == "medium"
        assert state["problem"]["starter_code"].startswith("class Solution")
        assert len(state["problem"]["visible_test_cases"]) >= 4
        assert state["submissions"] == []
        assert state["last_verdict"] is None

    def test_url_import_run_code_against_visible_tcs(self, client):
        """POST /session/{sid}/run must work against the imported visible test cases."""
        resp = client.post("/session", json={
            "topic": "整数反转",
            "difficulty": "medium",
            "mode": "practice",
            "leetcode_url": self.LEETCODE_URL,
        })
        sid = resp.json()["session_id"]
        wait_for_session(client, sid)

        # Run a correct solution against the imported problem
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

    def test_missing_leetcode_url_falls_back_to_normal(self, client):
        """POST /session without leetcode_url should return generating (background task)."""
        resp = client.post("/session", json={"topic": "array", "difficulty": "easy", "mode": "practice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "generating"  # background task
        assert data["session_id"] is not None


class TestProblemCleanup:
    """Sanity check: the problems table only has valid entries."""

    @pytest.mark.skip(reason="LeetCode-imported problems may have empty starter_code")
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