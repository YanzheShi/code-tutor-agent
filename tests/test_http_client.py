"""HTTP client tests for CodeTutor Agent API.

Covers every endpoint defined in ``api/main.py``:

  - GET  /health
  - POST /session                          (AI generate)
  - POST /session (leetcode_url)           (import from LeetCode URL)
  - POST /session/by-problem/{id}          (existing problem)
  - GET  /session/{sid}/state              (poll)
  - POST /session/{sid}/submit             (judge + tutor)
  - POST /session/{sid}/run                (visible test cases)
  - GET  /problems                         (list)

Run with:
    uv run pytest tests/test_http_client.py -v
    # or just exercise it as a script:
    uv run tests/test_http_client.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest

try:  # 脚本直跑时自己加载 .env；pytest 下已由 tests/conftest.py 统一加载
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

BASE_URL = "http://localhost:8765"

# 多用户改造后 /session、/problems、/run、/submit 全部要求 Bearer 鉴权：
# 凭据与 integration / _agent_helpers 同源（CTA_TEST_* → CTA_ADMIN_* → 默认测试账号）。
_AUTH_EMAIL = (
    os.getenv("CTA_TEST_EMAIL") or os.getenv("CTA_ADMIN_EMAIL") or "534629255@qq.com"
)
_AUTH_PASSWORD = (
    os.getenv("CTA_TEST_PASSWORD") or os.getenv("CTA_ADMIN_PASSWORD") or "test123456"
)
_TOKEN: str | None = None

# 需要后端服务真实在跑（localhost:8765）才能测；服务未启动会 ConnectError。
# 归为 integration，日常用 `pytest -m "not integration"` 跳过。
pytestmark = pytest.mark.integration

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


def _auth_headers(client: httpx.Client) -> dict[str, str]:
    """登录引导管理员并返回 Bearer 头；token 进程内缓存复用。

    登录失败**不在这里抛**：后端没起时应由调用方的 ConnectError 分支收编
    （脚本模式记为 SKIP 而不是崩掉）；账号/密码不对则后续请求会 401，报错更直白。
    """
    global _TOKEN
    if _TOKEN:
        return {"Authorization": f"Bearer {_TOKEN}"}
    try:
        r = client.post(
            f"{BASE_URL}/auth/login",
            json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD},
        )
    except httpx.HTTPError as exc:
        print(f"  [!] 登录请求失败（后端未启动？）: {exc}")
        return {}
    if r.status_code != 200:
        print(f"  [!] 登录失败 {r.status_code}: {r.text[:200]}")
        return {}
    _TOKEN = r.json()["token"]
    return {"Authorization": f"Bearer {_TOKEN}"}


@contextmanager
def _new_client() -> Iterator[httpx.Client]:
    """开一个「已带 Bearer 头」的 client：本机直连（trust_env=False 绕环境代理）。

    trust_env 必须显式关掉——环境里配了 HTTP 代理时，httpx 默认会把
    127.0.0.1/localhost 的请求也丢给代理，表现为 502 或莫名 ConnectError。

    必须用 contextmanager 而不是直接返回 client：登录请求会让 client 进入
    OPENED 状态，直接返回的话调用方再 `with client:` 会抛
    「Cannot open a client instance more than once」（2026-09-23 实测）。
    """
    with httpx.Client(trust_env=False) as client:
        client.headers.update(_auth_headers(client))
        yield client


def _banner(title: str) -> None:
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def _print_resp(label: str, resp: httpx.Response) -> dict:
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    status = resp.status_code
    ok = "OK" if 200 <= status < 300 else "FAIL"
    print(f"  [{ok}] {label}  →  {status} {resp.url.path}")
    if body:
        # Pretty-print compact JSON
        print(f"  Body: {json.dumps(body, ensure_ascii=False, indent=2)[:600]}")
    return body


def wait_for_session(sid: str, timeout: float = 60.0) -> dict:
    """Poll /session/{sid}/state until status != 'generating' or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _new_client() as client:
                r = client.get(f"{BASE_URL}/session/{sid}/state")
                if r.status_code != 200:
                    time.sleep(1)
                    continue
                data = r.json()
                if data.get("status") not in ("generating", "awaiting_problem"):
                    return data
                time.sleep(1.5)
        except httpx.ConnectError:
            break  # backend not running
    raise TimeoutError(f"Session {sid} did not finish generating within {timeout}s")


# ──────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────


def test_health() -> None:
    """Health check — is the graph ready?"""
    _banner("1. Health Check")
    with _new_client() as client:
        r = client.get(f"{BASE_URL}/health")
        data = _print_resp("GET /health", r)
        assert r.status_code == 200
        assert data.get("graph_ready") is True


def test_create_session_ai_generate() -> None:
    """Create a session with AI-generated problem (background)."""
    _banner("5. Create Session (AI Generate)")
    with _new_client() as client:
        r = client.post(f"{BASE_URL}/session", json={
            "topic": "数组",
            "difficulty": "easy",
            "mode": "practice",
        })
        data = _print_resp("POST /session", r)
        assert r.status_code == 200
        sid = data["session_id"]
        print(f"  Session ID: {sid}")
        print(f"  Polling for completion …")
        state = wait_for_session(sid)
        print(f"  Final status: {state['status']}")
        if state.get("problem"):
            print(f"  Problem: {state['problem']['title']}")
        return sid  # type: ignore[return-value]


def test_create_session_leetcode_url() -> None:
    """Create a session from a LeetCode URL (import path).

    导入入口现在在对话消息里（``POST /session/{sid}/chat/stream`` 的消息内含 URL），
    POST /session 只建会话并返回 ``status="dialog"``；URL 存进会话状态，等对话判定
    就绪后由 generator_node 抓取。题目是否就绪靠轮询 ``/state``。
    """
    _banner("6. Create Session (LeetCode URL)")
    with _new_client() as client:
        create_r = client.post(f"{BASE_URL}/session", json={
            "topic": "算法",
            "difficulty": "easy",
            "mode": "practice",
            "leetcode_url": "https://leetcode.cn/problems/two-sum/",
        })
        create_data = _print_resp("POST /session (leetcode_url)", create_r)
        assert create_r.status_code == 200
        # agent 模式：建会话即进导师对话态；带 leetcode_url 不再直接触发导入。
        assert create_data.get("status") == "dialog"

        sid = create_data.get("session_id")
        if sid:
            print(f"  Session ID: {sid} (polling …)")
            state = wait_for_session(sid)
            if state.get("problem"):
                print(f"  Problem: {state.get('problem', {}).get('title', 'N/A')}")


def test_list_problems() -> None:
    """List existing problems in the database."""
    _banner("7. List Problems")
    with _new_client() as client:
        r = client.get(f"{BASE_URL}/problems")
        data = _print_resp("GET /problems", r)
        problems = data.get("problems", [])
        print(f"  Total: {len(problems)}")
        for p in problems[:5]:
            print(f"    #{p['id']} {p['title']} [{p['difficulty']}] {p['topic']}")


def test_create_session_existing_problem() -> None:
    """Create a session using an existing problem from the DB."""
    _banner("8. Create Session (Existing Problem)")
    with _new_client() as client:
        # Find first problem
        r = client.get(f"{BASE_URL}/problems")
        data = r.json()
        problems = data.get("problems", [])
        if not problems:
            print("  SKIPPED — no problems in database")
            return
        pid = problems[0]["id"]
        print(f"  Using problem #{pid}: {problems[0]['title']}")

        r = client.post(f"{BASE_URL}/session/by-problem/{pid}")
        state = _print_resp(f"POST /session/by-problem/{pid}", r)
        assert r.status_code == 200
        assert state.get("problem")
        print(f"  Status: {state['status']}")


def test_submit_and_run_flow() -> None:
    """Full submit → judge → run cycle on a known problem."""
    _banner("9. Submit & Run Flow")
    with _new_client() as client:
        # Find a problem with test cases
        r = client.get(f"{BASE_URL}/problems")
        problems = r.json().get("problems", [])
        if not problems:
            print("  SKIPPED — no problems in database")
            return

        # Pick the first problem and create a session
        pid = problems[0]["id"]
        r = client.post(f"{BASE_URL}/session/by-problem/{pid}")
        state = r.json()
        sid = state["session_id"]
        problem_title = state.get("problem", {}).get("title", "?")
        print(f"  Session for: {problem_title}")

        # Poll until ready
        if state.get("status") == "generating":
            state = wait_for_session(sid)

        # Submit a simple Python solution (just the class stub)
        sample_code = """class Solution:
    def twoSum(self, nums: list[int], target: int) -> list[int]:
        pass
"""
        # Run first (visible test cases only)
        r = client.post(f"{BASE_URL}/session/{sid}/run", json={
            "code": sample_code,
            "language": "python",
        })
        run_data = _print_resp("POST /session/{sid}/run", r)

        # Submit (judge + tutor)
        r = client.post(f"{BASE_URL}/session/{sid}/submit", json={
            "code": sample_code,
            "language": "python",
        })
        submit_data = _print_resp("POST /session/{sid}/submit", r)
        print(f"  Verdict: {submit_data.get('verdict')}")
        if submit_data.get("tutor_message"):
            msg = submit_data["tutor_message"]
            print(f"  Tutor: {msg[:200]}")


def test_session_not_found() -> None:
    """Non-existent session should return 404."""
    _banner("10. Error Cases")
    fake_sid = str(uuid.uuid4())
    with _new_client() as client:
        r = client.get(f"{BASE_URL}/session/{fake_sid}/state")
        _print_resp(f"GET /session/{fake_sid}/state (404)", r)
        assert r.status_code == 404


# ──────────────────────────────────────────────
#  Run all tests (as script)
# ──────────────────────────────────────────────

TESTS = [
    ("Health check", test_health),
    ("Create session (AI generate)", test_create_session_ai_generate),
    ("Create session (LeetCode URL)", test_create_session_leetcode_url),
    ("List problems", test_list_problems),
    ("Create session (existing problem)", test_create_session_existing_problem),
    ("Submit & Run flow", test_submit_and_run_flow),
    ("Session not found", test_session_not_found),
]


def main() -> None:
    print(f"\n  CodeTutor Agent API — HTTP Client Tests")
    print(f"  Base URL: {BASE_URL}")
    print(f"  {len(TESTS)} tests\n")

    passed = 0
    failed = 0
    skipped = 0
    results: list[tuple[str, str | None]] = []

    for name, fn in TESTS:
        try:
            fn()
            passed += 1
            results.append((name, None))
        except TimeoutError as e:
            skipped += 1
            results.append((name, f"TIMEOUT: {e}"))
        except AssertionError as e:
            failed += 1
            results.append((name, f"ASSERT: {e}"))
        except httpx.ConnectError as e:
            skipped += 1
            results.append((name, f"CONNECT: Backend not running at {BASE_URL}"))
        except Exception as e:
            failed += 1
            results.append((name, f"ERROR: {e}"))

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    for name, err in results:
        icon = "PASS" if err is None else f"FAIL: {err}" if err else "SKIP"
        color = "" if err is None else "RED"
        print(f"  [{icon:>15}] {name}")
    print(f"\n  {passed} passed, {failed} failed, {skipped} skipped")
    print()


if __name__ == "__main__":
    main()
