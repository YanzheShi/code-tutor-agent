"""Judge0 REST API client — wraps the WSL Judge0 CE service.

Provides two entry points:
  1. ``run_code(source_code, stdin)`` — 1:1 Judge0 submission, returns raw result.
  2. ``submit_test_cases(source_code, test_cases)`` — batch run with harness,
     returns list[dict] compatible with ``RunnerResult`` / ``JudgeResult``.

Both pass ``enable_per_process_and_thread_time_limit=true`` and
``enable_per_process_and_thread_memory_limit=true`` so isolate runs with
soft limits (``-m``/``-t``) instead of cgroup-v1 ``--cg`` — works on WSL's
cgroup v2 without privileged mode.

Node flow:
    judge_node → submit_test_cases(user_code, test_cases)
    generator_node → submit_test_cases(brute_code, sample_tcs)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ── Tunables ──
JUDGE0_BASE_URL = os.getenv("JUDGE0_URL", "http://localhost:2358")
JUDGE0_PYTHON_ID = 71  # Judge0 CE Python 3.8.1
REQUEST_TIMEOUT = 15.0  # seconds (also Judge0's own wall-clock limit)
WAIT_TIMEOUT_SECONDS = 10  # Judge0 internal timeout for wait mode


# ── Pydantic-like result (plain dict for simplicity) ──


class Judge0SubmissionResult:
    """Normalised result from a Judge0 submission."""

    __slots__ = (
        "token", "stdout", "stderr", "compile_output", "message",
        "status_id", "status_desc", "time_seconds", "memory_kilobytes",
    )

    def __init__(self, raw: dict):
        self.token: str = raw.get("token", "")
        self.stdout: str = raw.get("stdout") or ""
        self.stderr: str = raw.get("stderr") or ""
        self.compile_output: str = raw.get("compile_output") or ""
        self.message: str = raw.get("message") or ""
        self.status_id: int = raw.get("status", {}).get("id", 0)
        self.status_desc: str = raw.get("status", {}).get("description", "")
        t = raw.get("time")
        self.time_seconds: float = float(t) if t else 0.0
        m = raw.get("memory")
        self.memory_kilobytes: float = float(m) if m else 0.0

    def is_accepted(self) -> bool:
        """status_id == 3 means Accepted."""
        return self.status_id == 3

    def verdict(self) -> str:
        """Map Judge0 status_id to our verdict string."""
        mapping = {
            3: "AC",
            4: "WA",   # Wrong Answer — Judge0 calls it "Wrong Answer"
            5: "TLE",  # Time Limit Exceeded
            6: "CE",   # Compilation Error
            7: "RE",   # Runtime Error (SIGSEGV, SIGFPE, etc.)
            8: "RE",   # Internal Error (should not happen with our fix)
            11: "RE",  # Runtime Error (other)
            12: "RE",  # Internal Error
            13: "CE",  # Compile Error (Isolate box creation)
            14: "RE",  # Runtime Error (other)
        }
        return mapping.get(self.status_id, "RE")

    def runtime_ms(self) -> float:
        return self.time_seconds * 1000.0


# ── Helpers ──


def _build_test_case_harness(source_code: str, test_cases: list[dict]) -> str:
    """Wrap LeetCode-style ``class Solution`` code for stdio-based Judge0.

    The harness encodes all test cases as the first line of stdin (JSON),
    runs each through the Solution method, and prints ``RESULT: <json>``
    lines on stdout — same format the local harness uses.

    This is the key adapter: Judge0 feeds data via stdin, so we serialise
    the entire test batch into the first line, then parse it in-memory.
    """
    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE

    tc_json = json.dumps(test_cases)

    return f"""\
{INJECT_PROLOGUE}
import ast, json, sys, time, inspect

# --- User / solution code ---
{source_code}

# --- Read all test cases from stdin ---
test_cases_raw = sys.stdin.readline()
test_cases = json.loads(test_cases_raw)

def _eval_arg(s):
    try:
        return ast.literal_eval(s)
    except Exception:
        return s

def _fmt(val):
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val), separators=(",", ":"))
    if isinstance(val, set):
        return json.dumps(sorted(val))
    return str(val)

# Discover the first public method on Solution
sol = Solution()
members = inspect.getmembers(sol, predicate=inspect.ismethod)
public = [(n, fn) for n, fn in members if not n.startswith('_')]
if not public:
    print('RESULT: ' + json.dumps({{"test_case_id": -1, "status": "Runtime Error", "detail": "no public method"}}))
    sys.exit(0)

method_name, method_fn = public[0]

for idx, tc in enumerate(test_cases):
    args = [_eval_arg(a) for a in tc.get('input_args', [])]
    expected = tc.get('expected_output', '')
    start = time.perf_counter()
    try:
        result = method_fn(*args)
        elapsed = (time.perf_counter() - start) * 1000
        actual = _fmt(result)
        # Normalise expected: parse JSON string to compare by value
        exp_raw = expected
        try:
            exp_val = json.loads(exp_raw) if isinstance(exp_raw, str) else exp_raw
            expected_fmt = _fmt(exp_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            expected_fmt = _fmt(exp_raw)
        if actual == expected_fmt:
            print('RESULT: ' + json.dumps({{
                "test_case_id": idx, "status": "Passed",
                "detail": actual, "runtime_ms": round(elapsed, 2)
            }}))
        else:
            print('RESULT: ' + json.dumps({{
                "test_case_id": idx, "status": "Wrong Answer",
                "detail": "expected=" + repr(expected_fmt) + " got=" + repr(actual),
                "runtime_ms": round(elapsed, 2)
            }}))
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        print('RESULT: ' + json.dumps({{
            "test_case_id": idx, "status": "Runtime Error",
            "detail": str(exc)[:200], "runtime_ms": round(elapsed, 2)
        }}))
        """


def _call_api(submission: dict) -> dict:
    """POST /submissions?wait=true and return parsed JSON."""
    url = f"{JUDGE0_BASE_URL.rstrip('/')}/submissions?base64_encoded=false&wait=true"
    # Always enforce the cgroup-v2-safe parameters
    submission.setdefault("enable_per_process_and_thread_time_limit", True)
    submission.setdefault("enable_per_process_and_thread_memory_limit", True)
    submission.setdefault("cpu_time_limit", 2.0)
    submission.setdefault("wall_time_limit", 5.0)
    submission.setdefault("memory_limit", 256000)  # KB

    data = json.dumps(submission).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error("Judge0 HTTP %d: %s", exc.code, body)
        raise RuntimeError(f"Judge0 API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        logger.error("Judge0 unreachable: %s", exc.reason)
        raise RuntimeError(f"Judge0 unreachable: {exc.reason}") from exc


# ── Public API ──


def run_code(
    source_code: str,
    stdin: str = "",
    language_id: int = JUDGE0_PYTHON_ID,
) -> Judge0SubmissionResult:
    """Execute arbitrary source code via Judge0.

    Args:
        source_code: Complete Python source (not wrapped).
        stdin: Standard input content.
        language_id: Judge0 language id (default 71 = Python 3.8.1).

    Returns:
        Normalised ``Judge0SubmissionResult``.
    """
    logger.info("▶ judge0.run_code() — %d chars, lang=%d", len(source_code), language_id)
    raw = _call_api({
        "source_code": source_code,
        "language_id": language_id,
        "stdin": stdin,
    })
    return Judge0SubmissionResult(raw)


def submit_test_cases(
    source_code: str,
    test_cases: list[dict],
    language_id: int = JUDGE0_PYTHON_ID,
) -> list[dict]:
    """Run LeetCode-style code against a batch of test cases via Judge0.

    Wraps the user code with a harness that reads test cases from stdin,
    runs them, and emits ``RESULT:`` lines.  Returns a list of dicts
    compatible with ``RunnerResult.to_dict()`` / ``JudgeResult``.

    Args:
        source_code: User's ``class Solution: ...`` code.
        test_cases: List of dicts with ``input_args`` and ``expected_output``.
        language_id: Judge0 language id.

    Returns:
        List of result dicts, one per test case.
        Each dict::
            {"test_case_id": int, "status": str, "detail": str, "runtime_ms": float}
    """
    if not test_cases:
        return []

    n = len(test_cases)
    logger.info("▶ judge0.submit_test_cases() — %d test cases, %d chars", n, len(source_code))

    harness = _build_test_case_harness(source_code, test_cases)
    stdin = json.dumps(test_cases)

    try:
        result = _call_api({
            "source_code": harness,
            "language_id": language_id,
            "stdin": stdin,
        })
    except RuntimeError as exc:
        # API unreachable — return structured errors
        logger.error("judge0.submit_test_cases() failed: %s", exc)
        return [
            {"test_case_id": i, "status": "Judge Error", "detail": str(exc)[:200], "runtime_ms": 0.0}
            for i in range(n)
        ]

    sr = Judge0SubmissionResult(result)

    # If Judge0 itself failed before the harness ran (CE / RE / Internal Error)
    if not sr.is_accepted():
        # Map Judge0 statuses to RunnerResult-compatible status strings
        # (the local harness outputs "Runtime Error" for syntax errors, so CE→Runtime Error)
        status_map = {5: "TLE", 6: "Runtime Error", 7: "Runtime Error",
                      8: "Runtime Error", 12: "Runtime Error", 13: "Runtime Error", 14: "Runtime Error"}
        verdict = status_map.get(sr.status_id, "Runtime Error")
        detail = sr.compile_output or sr.stderr or sr.message or sr.status_desc
        return [
            {"test_case_id": i, "status": verdict, "detail": detail[:200], "runtime_ms": sr.runtime_ms()}
            for i in range(n)
        ]

    # Parse harness RESULT: lines from stdout
    results: list[dict] = []
    for line in sr.stdout.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            try:
                data = json.loads(line[len("RESULT:"):])
                # Inject Judge0 memory info into each result
                data["memory_kb"] = sr.memory_kilobytes
                results.append(data)
            except json.JSONDecodeError:
                continue

    if not results:
        return [
            {"test_case_id": i, "status": "Judge Error", "detail": "No structured output from harness", "runtime_ms": 0.0}
            for i in range(n)
        ]

    return results


def check_health() -> dict:
    """Simple health check: return worker count and version."""
    url = f"{JUDGE0_BASE_URL.rstrip('/')}/workers"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            workers = json.loads(resp.read().decode("utf-8"))
            return {"workers_alive": len(workers), "api_version": "v1.13.1", "status": "ok"}
    except Exception as exc:
        return {"workers_alive": 0, "api_version": "unknown", "status": "error", "detail": str(exc)}


def list_languages() -> list[dict]:
    """List all supported languages from Judge0."""
    url = f"{JUDGE0_BASE_URL.rstrip('/')}/languages"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Failed to fetch languages: %s", exc)
        return []