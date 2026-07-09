"""Tests for the Agent Judge agent — LLM-driven judge analysis.

Coverage:
    1. format_results_for_prompt() — formats RunnerResult list
    2. analyze_judge_results() — LLM analysis with fallback
    3. JudgeAnalysis model — structured output
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_tutor_agent.agents.agent_judge import (
    JudgeAnalysis,
    analyze_judge_results,
    format_results_for_prompt,
)
from code_tutor_agent.sandbox.runner import RunnerResult


class TestFormatResults:
    """Formatting Judge0 results for the LLM prompt."""

    def test_formats_passed_result(self):
        results = [RunnerResult(0, "Passed", "5", runtime_ms=12.3)]
        text = format_results_for_prompt(results)
        assert "✅" in text
        assert "Passed" in text
        assert "12.3ms" in text

    def test_formats_failed_result_with_detail(self):
        results = [
            RunnerResult(0, "Wrong Answer", "expected=5 got=3", runtime_ms=5.0),
        ]
        text = format_results_for_prompt(results)
        assert "❌" in text
        assert "Wrong Answer" in text
        assert "expected=5 got=3" in text

    def test_multiple_results(self):
        results = [
            RunnerResult(0, "Passed", "1", runtime_ms=1.0),
            RunnerResult(1, "Passed", "2", runtime_ms=2.0),
            RunnerResult(2, "Wrong Answer", "expected=3 got=4", runtime_ms=3.0),
        ]
        text = format_results_for_prompt(results)
        lines = text.strip().split("\n")
        assert len(lines) >= 3  # at least 3 lines for 3 cases

    def test_empty_result_list(self):
        text = format_results_for_prompt([])
        assert text == ""


class TestAnalyzeJudgeResults:
    """LLM-driven analysis with fallback behavior."""

    SAMPLE_CODE = """class Solution:
    def solve(self, nums, target):
        seen = {}
        for i, n in enumerate(nums):
            if target - n in seen:
                return [seen[target - n], i]
            seen[n] = i
        return []
"""

    def test_fallback_on_llm_error(self):
        """When LLM fails, fallback should derive verdict mechanically."""
        results = [
            RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
            RunnerResult(1, "Passed", "[1,2]", runtime_ms=3.0),
        ]

        with patch("code_tutor_agent.agents.agent_judge.get_llm") as mock_get:
            mock_get.side_effect = Exception("LLM unavailable")

            analysis = analyze_judge_results(
                code=self.SAMPLE_CODE,
                title="两数之和",
                difficulty="easy",
                topic="数组",
                description="Find two numbers that sum to target",
                results=results,
            )

            assert isinstance(analysis, JudgeAnalysis)
            assert analysis.verdict == "AC"
            assert analysis.should_retry is False
            assert "恭喜" in analysis.warm_feedback

    def test_fallback_with_failures(self):
        """Fallback should mention which cases failed."""
        results = [
            RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
            RunnerResult(1, "Wrong Answer", "expected=[1,2] got=[0,0]", runtime_ms=2.0),
        ]

        with patch("code_tutor_agent.agents.agent_judge.get_llm") as mock_get:
            mock_get.side_effect = Exception("LLM unavailable")

            analysis = analyze_judge_results(
                code=self.SAMPLE_CODE,
                title="两数之和",
                difficulty="easy",
                topic="数组",
                description="",
                results=results,
            )

            assert analysis.verdict == "WA"
            assert analysis.should_retry is True
            assert "expected=[1,2]" in analysis.repair_suggestion

    def test_llm_analysis_all_pass(self):
        """LLM should return AC when all tests pass."""
        results = [
            RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
            RunnerResult(1, "Passed", "[1,2]", runtime_ms=3.0),
        ]

        mock_structured = MagicMock()
        mock_structured.invoke.return_value = JudgeAnalysis(
            verdict="AC",
            warm_feedback="恭喜！你的代码通过了所有测试 🎉",
            repair_suggestion="可以试试优化空间复杂度",
            should_retry=False,
        )

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_judge.get_llm", return_value=mock_llm):
            analysis = analyze_judge_results(
                code=self.SAMPLE_CODE,
                title="两数之和",
                difficulty="easy",
                topic="数组",
                description="",
                results=results,
            )

            assert analysis.verdict == "AC"
            assert analysis.should_retry is False
            assert "恭喜" in analysis.warm_feedback

    def test_llm_analysis_with_failures(self):
        """LLM should return WA with repair suggestion."""
        results = [
            RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0),
            RunnerResult(1, "Wrong Answer", "expected=[1,2] got=[0,0]", runtime_ms=2.0),
        ]

        mock_structured = MagicMock()
        mock_structured.invoke.return_value = JudgeAnalysis(
            verdict="WA",
            warm_feedback="别灰心，大部分用例通过了！",
            repair_suggestion="第2个用例：检查双指针的移动条件",
            should_retry=True,
        )

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_judge.get_llm", return_value=mock_llm):
            analysis = analyze_judge_results(
                code=self.SAMPLE_CODE,
                title="两数之和",
                difficulty="easy",
                topic="数组",
                description="",
                results=results,
            )

            assert analysis.verdict == "WA"
            assert analysis.should_retry is True
            assert "双指针" in analysis.repair_suggestion