"""Static problem pool — fallback when the generator's self-verification loop
exhausts its retries.

Each entry is a minimal dict in the exact shape returned by the LLM generator,
so the rest of the pipeline (save_problem, judge, etc.) works with no changes.
"""

from __future__ import annotations

import json
import os
from typing import Any

_POOL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "static_problems.json")


def _load_pool() -> list[dict[str, Any]]:
    """Load the static problem pool from disk."""
    if not os.path.exists(_POOL_PATH):
        _write_default_pool()
    with open(_POOL_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_default_pool() -> None:
    """Create the default static pool with a few classic problems."""
    pool = [
        {
            "title": "两数之和",
            "description": "给定一个整数数组 `nums` 和一个整数目标值 `target`，请你在该数组中找出和为目标值的那两个整数的下标。\n\n你可以假设每种输入只会对应一个答案。但是，数组中同一个元素不能使用两遍。\n\n你可以按任意顺序返回答案。",
            "difficulty": "easy",
            "topic": "数组+哈希表",
            "examples": [
                "输入: nums = [2,7,11,15], target = 9 → 输出: [0, 1]",
                "输入: nums = [3,2,4], target = 6 → 输出: [1, 2]",
                "输入: nums = [3,3], target = 6 → 输出: [0, 1]",
            ],
            "constraints": ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9", "-10^9 <= target <= 10^9"],
            "test_cases": [
                {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0, 1]", "is_hidden": False, "explanation": "基本正常输入"},
                {"input_args": ["[3,2,4]", "6"], "expected_output": "[1, 2]", "is_hidden": False, "explanation": "无序数组"},
                {"input_args": ["[3,3]", "6"], "expected_output": "[0, 1]", "is_hidden": True, "explanation": "重复元素"},
                {"input_args": ["[-1,-2,-3,-4,-5]", "-8"], "expected_output": "[2, 4]", "is_hidden": True, "explanation": "负数"},
                {"input_args": ["[0,4,3,0]", "0"], "expected_output": "[0, 3]", "is_hidden": False, "explanation": "含零"},
            ],
            "optimal_solution": "class Solution:\n    def solve(self, nums, target):\n        seen = {}\n        for i, num in enumerate(nums):\n            complement = target - num\n            if complement in seen:\n                return [seen[complement], i]\n            seen[num] = i\n        return []\n",
            "brute_solution": "class Solution:\n    def solve(self, nums, target):\n        for i in range(len(nums)):\n            for j in range(i + 1, len(nums)):\n                if nums[i] + nums[j] == target:\n                    return [i, j]\n        return []\n",
            "adversarial_spec": {"scale_description": "随机整数数组，target 靠后位置避免 early exit", "n": 50000, "data_type": "int"},
            "time_complexity": "O(n)",
            "space_complexity": "O(n)",
            "novelty_score": 4.0,
        },
        {
            "title": "最大子数组和",
            "description": "给你一个整数数组 `nums`，请你找出一个具有最大和的连续子数组（子数组最少包含一个元素），返回其最大和。\n\n子数组是数组中的一个连续部分。",
            "difficulty": "medium",
            "topic": "动态规划",
            "examples": [
                "输入: nums = [-2,1,-3,4,-1,2,1,-5,4] → 输出: 6 （子数组 [4,-1,2,1] 的和最大）",
                "输入: nums = [1] → 输出: 1",
                "输入: nums = [5,4,-1,7,8] → 输出: 23",
            ],
            "constraints": ["1 <= nums.length <= 10^5", "-10^4 <= nums[i] <= 10^4"],
            "test_cases": [
                {"input_args": ["[-2,1,-3,4,-1,2,1,-5,4]"], "expected_output": "6", "is_hidden": False, "explanation": "LeetCode 经典用例"},
                {"input_args": ["[1]"], "expected_output": "1", "is_hidden": False, "explanation": "单元素"},
                {"input_args": ["[5,4,-1,7,8]"], "expected_output": "23", "is_hidden": True, "explanation": "全正数"},
                {"input_args": ["[-1]"], "expected_output": "-1", "is_hidden": True, "explanation": "单负数"},
                {"input_args": ["[-2,-1]"], "expected_output": "-1", "is_hidden": False, "explanation": "全负数"},
            ],
            "optimal_solution": "class Solution:\n    def solve(self, nums):\n        max_sum = cur = nums[0]\n        for n in nums[1:]:\n            cur = max(n, cur + n)\n            max_sum = max(max_sum, cur)\n        return max_sum\n",
            "brute_solution": "class Solution:\n    def solve(self, nums):\n        n = len(nums)\n        max_sum = nums[0]\n        for i in range(n):\n            s = 0\n            for j in range(i, n):\n                s += nums[j]\n                max_sum = max(max_sum, s)\n        return max_sum\n",
            "adversarial_spec": {"scale_description": "正负交替的 50000 长度数组", "n": 50000, "data_type": "int"},
            "time_complexity": "O(n)",
            "space_complexity": "O(1)",
            "novelty_score": 5.0,
        },
        {
            "title": "找出数组中的最大值",
            "description": "给定一个非空整数数组 `nums`，返回数组中的最大元素。\n\n注意：数组长度至少为 1。",
            "difficulty": "easy",
            "topic": "数组",
            "examples": [
                "输入: nums = [3, 1, 4, 1, 5, 9, 2, 6] → 输出: 9",
                "输入: nums = [-3, -1, -7] → 输出: -1",
            ],
            "constraints": ["1 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"],
            "test_cases": [
                {"input_args": ["[3,1,4,1,5,9,2,6]"], "expected_output": "9", "is_hidden": False, "explanation": "基本正数"},
                {"input_args": ["[-3,-1,-7]"], "expected_output": "-1", "is_hidden": False, "explanation": "全负数"},
                {"input_args": ["[42]"], "expected_output": "42", "is_hidden": True, "explanation": "单元素"},
                {"input_args": ["[-1000000000,0,1000000000]"], "expected_output": "1000000000", "is_hidden": True, "explanation": "极值"},
            ],
            "optimal_solution": "class Solution:\n    def solve(self, nums):\n        max_val = nums[0]\n        for n in nums[1:]:\n            if n > max_val:\n                max_val = n\n        return max_val\n",
            "brute_solution": "",
            "adversarial_spec": None,
            "time_complexity": "O(n)",
            "space_complexity": "O(1)",
            "novelty_score": 3.0,
        },
    ]
    os.makedirs(os.path.dirname(_POOL_PATH), exist_ok=True)
    with open(_POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def get_static_problem(topic: str | None = None, difficulty: str | None = None) -> dict | None:
    """Pick a static problem, optionally filtering by topic/difficulty.

    Returns None if the pool is empty.
    """
    pool = _load_pool()
    if not pool:
        return None

    candidates = pool
    if topic:
        candidates = [p for p in candidates if p.get("topic") == topic or topic in p.get("topic", "")]
    if difficulty:
        candidates = [p for p in candidates if p.get("difficulty") == difficulty]

    if not candidates:
        candidates = pool  # fallback to any

    import random
    return random.choice(candidates)


def get_static_problem_count() -> int:
    """Return the size of the static pool."""
    return len(_load_pool())