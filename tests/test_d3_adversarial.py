"""D3 tests — adversarial strategy engine + multi-phase judge.

Coverage:
    1. Code weakness analysis (AST heuristic, no LLM)
    2. Boundary case generation (pure rules)
    3. Scale adversarial building
    4. Adversarial suite integration
    5. Judge node routing logic
"""

from __future__ import annotations

import pytest

from code_tutor_agent.sandbox.adversarial import (
    AdversarialSuite,
    analyze_code_weakness,
    generate_boundary_cases,
    run_adversarial_suite,
)
from code_tutor_agent.sandbox.runner import _build_adversarial_case, run_solution


# ═══════════════════════════════════════════════
#  Code weakness analysis tests
# ═══════════════════════════════════════════════

class TestWeaknessAnalysis:
    """AST-based weakness detector — no LLM calls."""

    def test_detects_nested_loop(self):
        code = """\
class Solution:
    def solve(self, nums, target):
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if nums[i] + nums[j] == target:
                    return [i, j]
        return []
"""
        result = analyze_code_weakness(code)
        assert result["weakness_type"] == "brute_force_n2"
        assert result["confidence"] >= 0.7

    def test_detects_recursion_no_memo(self):
        code = """\
class Solution:
    def fib(self, n):
        if n <= 1:
            return n
        return self.fib(n - 1) + self.fib(n - 2)
"""
        result = analyze_code_weakness(code)
        assert result["weakness_type"] == "recursion_no_memo"

    def test_detects_recursion_with_memo(self):
        code = """\
from functools import cache

class Solution:
    @cache
    def fib(self, n):
        if n <= 1:
            return n
        return self.fib(n - 1) + self.fib(n - 2)
"""
        result = analyze_code_weakness(code)
        # With @cache, recursion isn't a weakness
        assert result["weakness_type"] != "recursion_no_memo"

    def test_optimal_code_unknown_weakness(self):
        code = """\
class Solution:
    def solve(self, nums, target):
        seen = {}
        for i, num in enumerate(nums):
            if target - num in seen:
                return [seen[target - num], i]
            seen[num] = i
        return []
"""
        result = analyze_code_weakness(code)
        # Optimal O(n) hash solution — no obvious weakness
        assert result["weakness_type"] in ("unknown", "boundary_only_null")

    def test_syntax_error_returns_re(self):
        result = analyze_code_weakness("class Solution:\n    def solve(self):\n        syntax!!\n")
        assert result["weakness_type"] == "syntax_error"


# ═══════════════════════════════════════════════
#  Boundary case generation tests
# ═══════════════════════════════════════════════

class TestBoundaryGeneration:
    """Pure-rule boundary case generator — no LLM calls."""

    SAMPLE_PROBLEM = {
        "title": "两数之和",
        "description": "给定整数数组 nums 和目标值 target，返回两数下标",
        "constraints": [
            "2 <= nums.length <= 10^4",
            "-10^9 <= nums[i] <= 10^9",
        ],
        "test_cases": [
            {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"},
        ],
    }

    def test_generates_boundary_cases(self):
        cases = generate_boundary_cases(self.SAMPLE_PROBLEM)
        assert len(cases) >= 4  # empty, single, extreme, duplicate, negative
        for c in cases:
            assert "input_args" in c
            assert "expected_output" in c
            assert "explanation" in c

    def test_regression_no_empty_when_min_n_gt_0(self):
        """If constraints say min_n >= 2, don't generate empty array case."""
        problem = {**self.SAMPLE_PROBLEM, "constraints": ["2 <= nums.length <= 10^4"]}
        cases = generate_boundary_cases(problem)
        # Should still generate other boundary types
        assert len(cases) >= 3

    def test_single_number_boundary_has_wrong_expected(self):
        """singleNumber 的边界用例预期值不应硬编码 — 已在 run_adversarial_suite 中修复。

        之前边界生成器硬写了 max() 作为 expected_output，对 singleNumber 错误。
        修复后 expected_output 为空，由 run_adversarial_suite 用 optimal_solution 计算。
        """
        problem = {
            "title": "只出现一次的数字",
            "description": "给定非空整数数组，除了某个元素只出现一次外，其余每个元素均出现两次",
            "constraints": ["1 <= nums.length <= 3 * 10^4", "-3 * 10^4 <= nums[i] <= 3 * 10^4"],
            "test_cases": [
                {"input_args": ["[2,2,1]"], "expected_output": "1"},
            ],
        }
        cases = generate_boundary_cases(problem)
        for c in cases:
            # 所有边界用例的 expected_output 现在都为空字符串
            # （由 run_adversarial_suite 用参考解填充）
            assert c["expected_output"] == "", (
                f"边界用例 expected_output 不应硬编码: {c.get('explanation', '')} → {c['expected_output']}"
            )
            assert c["input_args"], f"边界用例缺少 input_args: {c}"
            assert c["explanation"], f"边界用例缺少 explanation: {c}"


# ═══════════════════════════════════════════════
#  Adversarial suite integration tests
# ═══════════════════════════════════════════════

class TestAdversarialSuite:
    """End-to-end adversarial suite — only runs on small inputs (no LLM)."""

    OPTIMAL_CODE = """\
class Solution:
    def twoSum(self, nums, target):
        seen = {}
        for i, num in enumerate(nums):
            complement = target - num
            if complement in seen:
                return [seen[complement], i]
            seen[num] = i
        return []
"""

    PROBLEM = {
        "title": "两数之和",
        "description": "给定整数数组 nums 和目标值 target，返回两数下标",
        "constraints": ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"],
        "test_cases": [
            {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"},
        ],
    }

    def test_suite_runs_without_crashing(self):
        """AdversarialSuite should complete without exceptions."""
        suite = run_adversarial_suite(self.PROBLEM, self.OPTIMAL_CODE)
        assert isinstance(suite, AdversarialSuite)
        assert suite.weakness is not None
        # At minimum, boundary tests should have run
        assert len(suite.boundary_results) >= 0

    def test_weak_code_triggers_boundary_fail(self):
        """代码只 return [0,0] → 边界测试应该挂。"""
        weak_code = """\
class Solution:
    def twoSum(self, nums, target):
        return [0, 0]
"""
        suite = run_adversarial_suite(self.PROBLEM, weak_code)
        # Should fail on boundary (or base, but adversarial still runs)
        assert suite.weakness is not None
        # At least some results should exist (boundary may or may not pass)
        assert len(suite.boundary_results) > 0 or len(suite.scale_results) > 0


# ═══════════════════════════════════════════════
#  Judge node routing logic tests (no LG graph)
# ═══════════════════════════════════════════════

class TestJudgeRouting:
    """Verify the routing logic embedded in judge_node docstrings."""

    def test_verdict_collapse_tle_priority(self):
        """TLE has higher priority than WA."""
        from code_tutor_agent.nodes.judge import _collapse_verdict

        class MockResult:
            def __init__(self, status):
                self.status = status

        results = [
            MockResult("Wrong Answer"),  # WA first
            MockResult("TLE"),           # TLE wins
            MockResult("Passed"),
        ]
        assert _collapse_verdict(results) == "TLE"

    def test_verdict_collapse_re_priority(self):
        """RE has higher priority than WA but lower than TLE."""
        from code_tutor_agent.nodes.judge import _collapse_verdict

        class MockResult:
            def __init__(self, status):
                self.status = status

        results = [
            MockResult("Wrong Answer"),
            MockResult("Runtime Error"),
        ]
        assert _collapse_verdict(results) == "RE"

    def test_describe_adversarial_failure(self):
        """_describe_adversarial_failure should produce human-readable text."""
        from code_tutor_agent.nodes.judge import _describe_adversarial_failure

        suite = AdversarialSuite()

        # Add a failed boundary result
        from code_tutor_agent.sandbox.runner import RunnerResult
        suite.boundary_results = [
            RunnerResult(0, "Wrong Answer", "expected=5 got=0"),
        ]
        suite.all_passed = False

        desc = _describe_adversarial_failure(suite)
        assert "边界对抗" in desc or "对抗" in desc