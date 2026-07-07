"""Sandbox runner — executes reference solutions against test cases.

D2: Used by the generator's self-verification loop to validate
both optimal_solution (must pass) and brute_solution (must TLE
on large input).

Windows-compatible: uses subprocess + timeout instead of resource.RLIMIT.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

import re as _re
_RE_TEMP = _re.compile(r'File "[^"]+[\\/]tmp[^"]+\.py"')


def _clean_error(stderr: str) -> str:
    """Strip temp-file paths from a traceback and return the last error line."""
    if not stderr:
        return ""
    cleaned = _RE_TEMP.sub("line", stderr)
    lines = cleaned.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith(("Traceback", "File ", "  ", "^", "~")):
            return line[:200]
    return cleaned[-200:]


# ── Tunables ──
TIMEOUT_SECONDS = 5.0       # how long before TLE (per test-case suite)
HARNESS_TIMEOUT = 2.0       # outer subprocess timeout (includes startup + TLE guard)


class RunnerResult:
    """Result of running one solution against one test case."""

    def __init__(
        self,
        test_case_id: int,
        status: str,
        detail: str = "",
        runtime_ms: float = 0.0,
        memory_kb: float = 0.0,
    ):
        self.test_case_id = test_case_id
        self.status = status       # Passed / Wrong Answer / Runtime Error / TLE
        self.detail = detail
        self.runtime_ms = runtime_ms
        self.memory_kb = memory_kb

    def to_dict(self) -> dict:
        return {
            "test_case_id": self.test_case_id,
            "status": self.status,
            "detail": self.detail,
            "runtime_ms": self.runtime_ms,
            "memory_kb": self.memory_kb,
        }


def _extract_python_code(text: str) -> str:
    """Extract Python code from LLM response, stripping markdown fences."""
    if match := __import__("re").search(r"```(?:python)?\s*\n(.*?)```", text, __import__("re").DOTALL):
        return match.group(1).strip()
    return text.strip()


def _has_class_solution(text: str) -> bool:
    return "class Solution" in text


def run_solution(
    code: str,
    test_cases: list[dict],
    timeout: float = TIMEOUT_SECONDS,
) -> list[RunnerResult]:
    """Execute a reference solution against a batch of test cases.

    Uses Judge0 backend when the ``JUDGE0_URL`` env var is set and the
    service is reachable; falls back to local subprocess otherwise.

    Args:
        code: Python source (may be markdown-fenced).
        test_cases: List of dicts with ``input_args`` and ``expected_output``.
        timeout: Seconds per-case timeout.

    Returns:
        List of ``RunnerResult``, one per test case.
    """
    code = _extract_python_code(code)
    n = len(test_cases) or 1
    logger.info("▶ run_solution() — %d test cases, timeout=%.1fs", n, timeout)

    # ── Try Judge0 backend when JUDGE0_URL is configured ──
    judge0_url = os.getenv("JUDGE0_URL")
    if judge0_url:
        logger.info("  router → Judge0 (%s)", judge0_url)
        try:
            from code_tutor_agent.sandbox.judge0_client import submit_test_cases

            dict_results = submit_test_cases(code, test_cases)
            if dict_results and dict_results[0].get("status") != "Judge Error":
                return [RunnerResult(
                    test_case_id=r["test_case_id"],
                    status=r["status"],
                    detail=r.get("detail", ""),
                    runtime_ms=r.get("runtime_ms", 0.0),
                    memory_kb=r.get("memory_kb", 0.0),
                ) for r in dict_results]
            else:
                logger.warning("Judge0 returned errors, falling back to local subprocess")
        except Exception as exc:
            logger.warning("Judge0 routing failed (%s), falling back to local", exc)

    # ── Fallback: local subprocess ──
    logger.info("  router → local subprocess")
    harness = _build_harness(code, test_cases)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(harness)
        tmp.close()

        proc = subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout + HARNESS_TIMEOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        results: list[RunnerResult] = []
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                data = json.loads(line[len("RESULT:"):])
                results.append(RunnerResult(**data))

        # If harness produced no structured output, fallback
        if not results and proc.returncode != 0:
            detail = _clean_error(proc.stderr[:500])
            return [RunnerResult(i, "Runtime Error", detail) for i in range(n)]
        if not results:
            return [RunnerResult(i, "Judge Error", "No structured output") for i in range(n)]

        return results

    except subprocess.TimeoutExpired:
        return [RunnerResult(i, "TLE", f"timed out after {timeout}s") for i in range(n)]
    except Exception as exc:
        logger.error("Exception: %s", exc)
        return [RunnerResult(i, "Runtime Error", str(exc)) for i in range(n)]
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


def _build_harness(code: str, test_cases: list[dict]) -> str:
    """Build a standalone Python script that runs test cases against the code.

    Injects LeetCode types (from typing import *, TreeNode, ListNode, Node)
    into the global namespace before the user code.
    """
    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE

    tc_json = json.dumps(test_cases)

    return f"""\
{INJECT_PROLOGUE}
import ast, json, sys, time, inspect
import logging

logger = logging.getLogger(__name__)


# --- User / reference code ---
{code}

# --- Test cases ---
test_cases = json.loads({tc_json!r})

def _eval_arg(s):
    try:
        return ast.literal_eval(s)
    except Exception:
        return s

def _fmt(val):
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val), separators=(",", ":"))
    if isinstance(val, set):
        return json.dumps(sorted(val), separators=(",", ":"))
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
    args = [_eval_arg(a) for a in tc['input_args']]
    expected = tc['expected_output']
    start = time.perf_counter()
    try:
        result = method_fn(*args)
        elapsed = (time.perf_counter() - start) * 1000
        actual = _fmt(result)
        # Normalise expected: parse JSON string to compare by value
        try:
            exp_val = json.loads(expected) if isinstance(expected, str) else expected
            exp_fmt = _fmt(exp_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            exp_fmt = _fmt(expected)
        if actual == exp_fmt:
            print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Passed", "detail": actual, "runtime_ms": round(elapsed, 2)}}))
        else:
            print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Wrong Answer", "detail": f"expected={{exp_fmt}} got={{actual}}", "runtime_ms": round(elapsed, 2)}}))
    except Exception as exc:
        logger.error("Exception: %s", exc)
        elapsed = (time.perf_counter() - start) * 1000
        print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Runtime Error", "detail": str(exc)[:200], "runtime_ms": round(elapsed, 2)}}))
"""


def run_adversarial_check(
    brute_code: str,
    scale_spec: dict | None,
    timeout: float = TIMEOUT_SECONDS,
) -> RunnerResult | None:
    """Run brute_solution on a large adversarial input -> expect TLE."""
    if not brute_code or not scale_spec:
        return None

    n = scale_spec.get("n", 100_000)
    data_type = scale_spec.get("data_type", "int")
    scale_description = scale_spec.get("scale_description", "")
    large_case = _build_adversarial_case(n, data_type, scale_description)
    if not large_case:
        return None

    results = run_solution(brute_code, [large_case], timeout=timeout)
    return results[0] if results else None


def _build_adversarial_case(n: int, data_type: str, scale_description: str) -> dict | None:
    """Generate one large adversarial test case for O(n^2) brute force."""
    import random as rnd
    rnd.seed(42)

    n_actual = min(n, 20_000)
    target = 999999999
    a, b = 123456789, target - 123456789

    if "random" in scale_description.lower() or "分布" in scale_description:
        vals = [i * 2 + 1 for i in range(n_actual - 2)]
    elif "正负" in scale_description or "mixed" in scale_description.lower():
        vals = [(i * 2 + 1) * (1 if i % 2 == 0 else -1) for i in range(n_actual - 2)]
    else:
        vals = list(range(1, n_actual - 1))

    vals.append(a)
    vals.append(b)

    arr_str = "[" + ",".join(str(v) for v in vals) + "]"
    return {
        "input_args": [arr_str, str(target)],
        "expected_output": f"[{n_actual - 2}, {n_actual - 1}]",
    }