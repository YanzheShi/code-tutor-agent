"""Tests for the judge tool (LeetCode style)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from code_tutor_agent.tools.judge import run_code

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

CORRECT_CODE = """\
from typing import List

class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        seen = {}
        for i, num in enumerate(nums):
            complement = target - num
            if complement in seen:
                return [seen[complement], i]
            seen[num] = i
"""

WRONG_CODE = """\
from typing import List

class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        seen = {}
        for i, num in enumerate(nums):
            complement = target - num
            if complement in seen:
                return [i, seen[complement]]
            seen[num] = i
"""

ERROR_CODE = "class Solution(\n"

TEST_CASES = [
    {"input_args": ["[2, 7, 11, 15]", "9"], "expected_output": "[0,1]"},
    {"input_args": ["[3, 2, 4]", "6"], "expected_output": "[1,2]"},
    {"input_args": ["[3, 3]", "6"], "expected_output": "[0,1]"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_results(label: str, results: list) -> None:
    print(f"\n--- {label} ---")
    for r in results:
        extra = f"  {r.get('detail')}" if r.get("detail") else ""
        print(f"  TC{r['test_case_id']}: {r['status']}{extra}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_correct_code():
    results = run_code(CORRECT_CODE, TEST_CASES)
    print_results("正确代码", results)
    assert all(r["status"] == "Passed" for r in results), f"Expected all Passed, got: {results}"


def test_wrong_answer():
    results = run_code(WRONG_CODE, TEST_CASES)
    print_results("错误代码", results)
    assert any(r["status"] == "Wrong Answer" for r in results), f"Expected WA, got: {results}"


def test_runtime_error():
    results = run_code(ERROR_CODE, TEST_CASES)
    print_results("语法错误代码", results)
    assert all(r["status"] == "Runtime Error" for r in results), f"Expected RE, got: {results}"


if __name__ == "__main__":
    print("=" * 60)
    print("  验证 2（LeetCode 风格）：判题脚本能跑通")
    print("=" * 60)

    test_correct_code()
    test_wrong_answer()
    test_runtime_error()

    print("\n" + "=" * 60)
    print("  ✅ 验证 2 通过！判题机工作正常。")
    print("=" * 60)
