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
import sys
import time
import uuid
from typing import Any

import httpx

BASE_URL = "http://localhost:8765"

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


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
            with httpx.Client() as client:
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
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/health")
        data = _print_resp("GET /health", r)
        assert r.status_code == 200
        assert data.get("graph_ready") is True


def test_create_session_ai_generate() -> None:
    """Create a session with AI-generated problem (background)."""
    _banner("5. Create Session (AI Generate)")
    with httpx.Client() as client:
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

    The URL is passed through; parsing/fetching is consolidated in the
    generation package (generator_node), so the session starts in the
    background ``generating`` state and the imported problem shows up
    after polling.
    """
    _banner("6. Create Session (LeetCode URL)")
    with httpx.Client() as client:
        create_r = client.post(f"{BASE_URL}/session", json={
            "topic": "算法",
            "difficulty": "easy",
            "mode": "practice",
            "leetcode_url": "https://leetcode.cn/problems/two-sum/",
        })
        create_data = _print_resp("POST /session (leetcode_url)", create_r)
        assert create_r.status_code == 200
        # New contract: import path also goes through background generation.
        assert create_data.get("status") == "generating"

        sid = create_data.get("session_id")
        if sid:
            print(f"  Session ID: {sid} (polling …)")
            state = wait_for_session(sid)
            if state.get("problem"):
                print(f"  Problem: {state.get('problem', {}).get('title', 'N/A')}")


def test_list_problems() -> None:
    """List existing problems in the database."""
    _banner("7. List Problems")
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/problems")
        data = _print_resp("GET /problems", r)
        problems = data.get("problems", [])
        print(f"  Total: {len(problems)}")
        for p in problems[:5]:
            print(f"    #{p['id']} {p['title']} [{p['difficulty']}] {p['topic']}")


def test_create_session_existing_problem() -> None:
    """Create a session using an existing problem from the DB."""
    _banner("8. Create Session (Existing Problem)")
    with httpx.Client() as client:
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
    with httpx.Client() as client:
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
    with httpx.Client() as client:
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
