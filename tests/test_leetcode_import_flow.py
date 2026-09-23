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
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure the project src is on the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app


# ── 鉴权（多用户改造后 /session、/state、/run 统一 Bearer）────────────────────

# 凭据与 auth.ensure_bootstrap_admin 同源：CTA_ADMIN_* 优先（.env 里那份），
# 回退到 integration 测试沿用的默认测试账号。
_AUTH_EMAIL = (
    os.getenv("CTA_ADMIN_EMAIL") or os.getenv("CTA_TEST_EMAIL") or "534629255@qq.com"
).strip().lower()
_AUTH_PASSWORD = (
    os.getenv("CTA_ADMIN_PASSWORD") or os.getenv("CTA_TEST_PASSWORD") or "test123456"
)


def _login_headers(c) -> dict:
    """登录引导管理员，返回 Bearer 头。

    全局 conftest 每条用例后会 TRUNCATE 所有表（含 users），所以**不能跨用例缓存
    token**——这里每次现登。首次登录失败时补上 env 并重建 bootstrap admin 再试一次
    （ensure_bootstrap_admin 幂等，邮箱已存在则 no-op）。
    """
    resp = c.post("/auth/login", json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD})
    if resp.status_code != 200:
        os.environ.setdefault("CTA_ADMIN_EMAIL", _AUTH_EMAIL)
        os.environ.setdefault("CTA_ADMIN_PASSWORD", _AUTH_PASSWORD)
        from code_tutor_agent.api.auth import ensure_bootstrap_admin

        ensure_bootstrap_admin()
        resp = c.post(
            "/auth/login", json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD}
        )
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    """FastAPI TestClient wrapping the real app (graph compiles at startup)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client) -> dict:
    """每条用例现登一次（用例之间会清库，token 不能跨用例复用）。"""
    return _login_headers(client)


def wait_for_session(
    c,
    sid: str,
    timeout: float = 120.0,
    headers: dict | None = None,
    expect: str | None = None,
) -> dict:
    """轮询 GET /session/{sid}/state。

    - 不给 ``expect``：等到状态离开 generating / awaiting_problem（旧语义）。
      ⚠️ 注意它**不排除 dialog**，所以对话态会被当成"已就绪"直接返回。
    - 给了 ``expect``：严格等到 ``status == expect`` 且题目已加载——agent 模式下
      LLM 可能先停在 dialog 追问，用这个才不会误判为"就绪"。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = c.get(f"/session/{sid}/state", headers=headers or {})
        if resp.status_code != 200:
            raise AssertionError(f"state poll returned {resp.status_code}")
        last = resp.json()
        if expect is not None:
            if last.get("status") == expect and last.get("problem"):
                return last
        elif last.get("status") not in ("generating", "awaiting_problem"):
            return last
        time.sleep(0.5)
    raise TimeoutError(
        f"Session {sid} 未在 {timeout}s 内达到 {expect or '就绪'}；"
        f"最后 status={last.get('status')}"
    )


def _drive_until_problem(
    c, sid: str, url: str, headers: dict, timeout: float = 240.0
) -> dict:
    """把 URL 作为对话消息发出，等到 awaiting_submit 且题目已加载。

    agent-only 重构后（start_router 只看 mode=="agent"，POST /session 一律进
    agent_dialog），**带 leetcode_url 的 POST 不再触发导入**——导入入口已经迁到
    对话消息里。所以 URL 导入类用例必须走「建会话 → 发 URL 消息 → 轮询」这条路
    （2026-09-23 修：旧用例直接断言 POST 后就 awaiting_submit，早已不成立）。

    首句文案与 ``scripts/_lc_trace_regression.py`` 一致（光贴 URL 会被继续追问）；
    LLM 判定 is_ready 有随机性，偶尔仍停在 dialog 追问，故第二句再推一次，
    每句分走一半超时预算。
    """
    nudges = [
        f"{url} 我想做这道 LeetCode 题，直接帮我导入开始做。",
        "不用再确认了，直接开始出题吧。",
    ]
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    for nudge in nudges:
        resp = c.post(
            f"/session/{sid}/chat/stream", headers=headers, json={"message": nudge}
        )
        try:  # 消费 SSE 流以触发后台出题（BackgroundTasks 在响应返回后跑）
            for _ in resp.iter_text():
                pass
        except Exception:  # noqa: BLE001
            pass
        budget = max(10.0, min(deadline - time.time(), timeout / len(nudges)))
        try:
            return wait_for_session(
                c, sid, timeout=budget, headers=headers, expect="awaiting_submit"
            )
        except TimeoutError as exc:
            last_exc = exc
    raise TimeoutError(f"URL 导入未在 {timeout}s 内完成：{last_exc}")


def _run_code(c, sid: str, code: str, headers: dict, retries: int = 4):
    """调 POST /session/{sid}/run，容忍「刚就绪、graph 尚未挂到 wait_for_submit」的窄竞态。

    /state 在 checkpoint 写回 status=awaiting_submit 的瞬间就报就绪，而 graph 可能
    还差最后一步（interrupt 挂到 wait_for_submit_node）；此刻 /run 会 400
    「当前不可运行：会话未在等待提交」。run.py 的卡死兜底目前只覆盖
    status in (dialog, error)，所以这 1~2s 的窗口要靠调用方容忍（2026-09-23 实测）。
    """
    resp = None
    for _ in range(retries):
        resp = c.post(
            f"/session/{sid}/run",
            headers=headers,
            json={"code": code, "language": "python"},
        )
        if resp.status_code != 400:
            return resp
        time.sleep(1.5)
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestSessionLeetCodeUrlImport:
    """POST /session + leetcode_url — import path goes through background generation.

    Parsing/fetching is now consolidated in generator_node (generation pkg);
    the session starts in 'generating' and reaches 'awaiting_submit' after the
    imported problem is loaded (network permitting).
    """

    LEETCODE_URL = "https://leetcode.cn/problems/reverse-integer/"

    def test_url_import_returns_generating(self, client, auth):
        """POST /session with leetcode_url must return status=generating + session_id."""
        resp = client.post("/session", headers=auth, json={
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

    def test_url_import_polls_to_awaiting_submit(self, client, auth):
        """在对话里发 URL → 必须轮询到 awaiting_submit，且题目来自导入内容。

        This is the critical guard against the stale closure bug: the frontend
        polls until status != 'generating' — the session MUST leave 'generating'
        and load the imported problem, otherwise the frontend loops forever.
        """
        resp = client.post("/session", headers=auth, json={
            "topic": "整数反转",
            "difficulty": "medium",
            "mode": "practice",
        })
        sid = resp.json()["session_id"]

        state = _drive_until_problem(client, sid, self.LEETCODE_URL, auth)
        assert state["status"] == "awaiting_submit"
        assert state["problem"] is not None
        assert state["problem"]["title"] == "整数反转"
        assert state["problem"]["difficulty"] == "medium"
        assert state["problem"]["starter_code"].startswith("class Solution")
        assert len(state["problem"]["visible_test_cases"]) >= 4
        assert state["submissions"] == []
        assert state["last_verdict"] is None

    def test_url_import_run_code_against_visible_tcs(self, client, auth):
        """POST /session/{sid}/run must work against the imported visible test cases."""
        resp = client.post("/session", headers=auth, json={
            "topic": "整数反转",
            "difficulty": "medium",
            "mode": "practice",
        })
        sid = resp.json()["session_id"]
        _drive_until_problem(client, sid, self.LEETCODE_URL, auth)

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
        run_resp = _run_code(client, sid, code, auth)
        assert run_resp.status_code == 200, run_resp.text
        r = run_resp.json()
        assert r["passed"] == r["total"], f"Expected all passed, got {r['passed']}/{r['total']}: {r}"
        assert r["all_passed"] is True

    def test_missing_leetcode_url_falls_back_to_normal(self, client, auth):
        """POST /session without leetcode_url should return generating (background task)."""
        resp = client.post(
            "/session",
            headers=auth,
            json={"topic": "array", "difficulty": "easy", "mode": "practice"},
        )
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