"""Judge tool: execute LeetCode-style user code against test cases.

The judge writes the user code + test harness into a temp script and runs
it once.  Results are reported per test case.
"""

import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ── Helpers ──
def run_code(code: str, test_cases: List[Dict]) -> List[Dict]:
    """Execute a ``class Solution`` snippet against a batch of test cases.

    Args:
        code: User's Python code (typically ``class Solution: ...``).
        test_cases: List of dicts with ``input_args`` (list of arg strings)
                    and ``expected_output``.

    Returns:
        List of result dicts, one per test case.
    """
    logger.info("▶ run_code()")
    results: List[Dict] = []
    n = len(test_cases)

    harness = _build_harness(code, test_cases)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(harness)
        tmp_path = f.name

    try:
        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=2.0,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                data = json.loads(line[len("RESULT:"):])
                results.append(data)

        if not results and proc.returncode != 0:
            results = [
                {"test_case_id": i, "status": "Runtime Error", "detail": proc.stderr[:200]}
                for i in range(n)
            ]
        elif not results:
            results = [
                {"test_case_id": i, "status": "Judge Error", "detail": "No output from judge"}
                for i in range(n)
            ]

    except subprocess.TimeoutExpired:
        results = [{"test_case_id": i, "status": "Time Limit Exceeded"} for i in range(n)]
    except Exception as exc:
        logger.error("Exception: %s", exc)
        results = [{"test_case_id": i, "status": "Judge Error", "detail": str(exc)} for i in range(n)]
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return results


def _build_harness(code: str, test_cases: List[Dict]) -> str:
    """Build a single executable script that runs all test cases."""
    # Quick syntax check on user code
    try:
        ast.parse(code)
    except SyntaxError as exc:
        logger.error("Exception: %s", exc)
        detail_msg = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        result_line = f'print("RESULT: " + json.dumps({{"test_case_id": 0, "status": "Runtime Error", "detail": {detail_msg!r}}}))'
        return f"import json, sys\n{result_line}\n"

    # Embed test cases as a JSON literal — safe and injectable into Python source
    tc_json = json.dumps(test_cases)

    # Build the result-printing lines separately to avoid f-string nesting issues
    result_pass = 'print("RESULT: " + json.dumps({"test_case_id": idx, "status": "Passed", "detail": actual}))'
    result_wa = ('print("RESULT: " + json.dumps({"test_case_id": idx, "status": "Wrong Answer", '
                 '"detail": "expected=" + repr(expected_fmt) + " got=" + repr(actual)}))')
    result_re = ('print("RESULT: " + json.dumps({"test_case_id": idx, "status": "Runtime Error", '
                 '"detail": str(exc)}))')
    result_no_method = ('print("RESULT: " + json.dumps({"test_case_id": -1, '
                        '"status": "Runtime Error", "detail": "no public method found on Solution"}))')

    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE

    return f"""\
import ast
import json
import logging
import sys

# ===== LeetCode 类型注入 =====
{INJECT_PROLOGUE}

# --- User code ---
    {code}

# --- Test cases (embedded as JSON) ---
test_cases = json.loads({tc_json!r})

def _eval_arg(s):
    \"\"\"Safely evaluate a string as a Python literal.\"\"\"
    try:
        return ast.literal_eval(s)
    except Exception:
        return s

def _fmt(val):
    \"\"\"Normalize a return value for comparison.\"\"\"
    if isinstance(val, list):
        return json.dumps(val, separators=(",", ":"))
    if isinstance(val, tuple):
        return json.dumps(list(val), separators=(",", ":"))
    if isinstance(val, set):
        return json.dumps(sorted(val), separators=(",", ":"))
    return str(val)

# Discover the first public method on Solution
import inspect

logger = logging.getLogger(__name__)

sol = Solution()
members = inspect.getmembers(sol, predicate=inspect.ismethod)
public_methods = [(name, fn) for name, fn in members if not name.startswith('_')]

if not public_methods:
    {result_no_method}
    sys.exit(0)

method_name, method_fn = public_methods[0]

# Run each test case individually
for idx, tc in enumerate(test_cases):
    args = [_eval_arg(a) for a in tc['input_args']]
    expected = tc['expected_output']
    try:
        result = method_fn(*args)
        actual = _fmt(result)
        # Normalise expected: parse JSON string to compare by value
        try:
            exp_val = json.loads(expected) if isinstance(expected, str) else expected
            expected_fmt = _fmt(exp_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            expected_fmt = _fmt(expected)
        if actual == expected_fmt:
            {result_pass}
        else:
            {result_wa}
    except Exception as exc:
        logger.error("Exception: %s", exc)
        {result_re}
"""
