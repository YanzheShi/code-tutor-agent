"""D2 tests — self-verification loop + sandbox runner."""

from __future__ import annotations

import pytest

from code_tutor_agent.sandbox.runner import (
    _build_adversarial_case,
    run_solution,
)


class TestSandboxRunner:
    """Verify the reference-solution sandbox (no LLM needed)."""

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

    BRUTE_CODE = """\
class Solution:
    def twoSum(self, nums, target):
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if nums[i] + nums[j] == target:
                    return [i, j]
        return []
"""

    TEST_CASES = [
        {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"},
        {"input_args": ["[3,2,4]", "6"], "expected_output": "[1,2]"},
        {"input_args": ["[3,3]", "6"], "expected_output": "[0,1]"},
    ]

    def test_optimal_passes_all(self):
        """Optimal solution must pass all given test cases."""
        results = run_solution(self.OPTIMAL_CODE, self.TEST_CASES)
        assert len(results) == len(self.TEST_CASES)
        for r in results:
            assert r.status == "Passed", f"TC #{r.test_case_id}: {r.detail}"

    def test_brute_tles_on_large_input(self):
        """Brute-force solution must TLE on a 50k-sized adversarial case."""
        adv_case = _build_adversarial_case(
            n=50000,
            data_type="int",
            scale_description="random distribution, target at end",
        )
        assert adv_case is not None
        results = run_solution(self.BRUTE_CODE, [adv_case], timeout=1.5)
        # With 50k elements and a 1s timeout, O(n²) should definitely TLE
        if results:
            assert results[0].status == "TLE", f"Expected TLE, got {results[0].status}"

    def test_brute_passes_small(self):
        """Brute-force solution must pass on small inputs."""
        results = run_solution(self.BRUTE_CODE, self.TEST_CASES)
        assert len(results) == len(self.TEST_CASES)
        for r in results:
            assert r.status == "Passed", f"TC #{r.test_case_id}: {r.detail}"

    def test_syntax_error_returns_re(self):
        """Malformed code should return Runtime Error."""
        bad_code = "class Solution:\n    def solve(self):\n        syntax error here\n"
        results = run_solution(bad_code, [{"input_args": ["[]"], "expected_output": "0"}])
        assert len(results) > 0
        assert results[0].status in ("Runtime Error", "Judge Error")

    def test_wrong_answer_detected(self):
        """Code that returns wrong results should be flagged WA."""
        wrong_code = """\
class Solution:
    def twoSum(self, nums, target):
        return [0, 0]
"""
        results = run_solution(wrong_code, self.TEST_CASES)
        assert any(r.status == "Wrong Answer" for r in results)

    def test_adversarial_builder_returns_dict(self):
        """Adversarial case builder should return a valid test case dict."""
        case = _build_adversarial_case(100, "int", "random")
        assert case is not None
        assert "input_args" in case
        assert "expected_output" in case


class TestStaticPool:
    """Verify the static problem pool."""

    def test_pool_creates_on_first_access(self):
        from code_tutor_agent.store.static_pool import get_static_problem_count
        count = get_static_problem_count()
        assert count >= 3, f"Expected ≥3 problems, got {count}"

    def test_get_static_by_topic(self):
        from code_tutor_agent.store.static_pool import get_static_problem
        p = get_static_problem(topic="数组")
        assert p is not None
        assert "标题" in p.get("title", "") or "数组" in p.get("topic", "")

    def test_get_static_fallback(self):
        from code_tutor_agent.store.static_pool import get_static_problem
        p = get_static_problem()
        assert p is not None
        assert "optimal_solution" in p