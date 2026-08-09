"""针对 generator 路径 A(_generate_from_leetcode)的单元测试。

覆盖一个此前未被集成测试捕获的真实崩溃:
- 后台 graph.invoke 无 stream 上下文时,langgraph 的 get_stream_writer() 返回 None,
  函数必须自己定义 writer 并降级为 no-op,否则会 NameError / None callable 崩溃。
  (集成测试的 fast-path 走 POST /session 直接落库、不跑 graph.invoke,故未触发。)
- problem_dict 必须带 constraints 落库,供后续 _generate_complex_tests 生成边界用例。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.nodes import generator


def _make_lc_data() -> dict:
    return {
        "title": "Two Sum",
        "description": "Given an array of integers nums and an integer target...",
        "difficulty": "easy",
        "examples": ["输入: nums = [2,7,11,15], target = 9 → 输出: [0, 1]"],
        "starter_code": (
            "class Solution:\n"
            "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
            "        pass\n"
        ),
        "tags": ["array"],
        "hints": [],
        "parsed_test_cases": [
            {
                "input_args": ["[2,7,11,15]", "9"],
                "expected_output": "[0, 1]",
                "explanation": "基本正常输入",
            },
        ],
        "description_html": "<p>Two Sum</p>",
        "constraints": ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"],
    }


def test_generate_from_leetcode_handles_no_stream_writer():
    """后台 graph.invoke(无 stream writer)触发路径 A 时不崩,且 constraints 落库。"""
    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "save_problem", return_value=1) as mock_save, \
         patch.object(generator, "_generate_optimal_for_leetcode_sync") as mock_opt:
        mock_opt.return_value = None

        cmd = generator._generate_from_leetcode("sid-123", _make_lc_data())

        # 返回有效的 Command 路由到 wait_for_submit_node
        assert cmd is not None
        assert cmd.goto == "wait_for_submit_node"

        # save_problem 被调用,且 problem_dict 带 constraints
        assert mock_save.called
        saved = mock_save.call_args[0][0]
        assert saved["constraints"] == [
            "2 <= nums.length <= 10^4",
            "-10^9 <= nums[i] <= 10^9",
        ]
        # 可见用例直接用 parse_leetcode 的 parsed_test_cases(非空串重解析)
        assert saved["test_cases"][0]["expected_output"] == "[0, 1]"


def test_generate_from_leetcode_falls_back_when_no_parsed_tcs():
    """没有 parsed_test_cases 时退回 _parse_examples_to_test_cases(带 starter_code)。"""
    lc = _make_lc_data()
    lc.pop("parsed_test_cases")
    fallback_tc = {
        "input_args": ["[1]"],
        "expected_output": "[0]",
        "explanation": "fallback",
    }
    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "save_problem", return_value=1) as mock_save, \
         patch.object(generator, "_generate_optimal_for_leetcode_sync"), \
         patch.object(
             generator, "_parse_examples_to_test_cases",
             return_value=[fallback_tc],
         ) as mock_parse:
        cmd = generator._generate_from_leetcode("sid-456", lc)
        assert cmd is not None
        # 无 parsed_test_cases → 应走到 fallback 解析
        assert mock_parse.called
        saved = mock_save.call_args[0][0]
        # fallback 解析结果被采用
        assert saved["test_cases"][0]["expected_output"] == "[0]"
