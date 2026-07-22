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
    _deterministic_verdict,
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


class TestForcedVerdict:
    """verdict 永远以执行引擎客观结果为准，不被 LLM 主观判断覆盖（修复误判 WA）。

    复现场景：用户提交了正确代码（全部 Passed），但 LLM 读不懂实现，
    臆造「实际输出」并错误判 WA。强制 verdict 后，最终 verdict 必须为 AC。
    """

    CORRECT_CODE = """class Solution:
    def maxSubArray(self, nums: List[int]) -> int:
        cs = 0
        res = min(nums)
        for i, x in enumerate(nums):
            cs = max(x, x + (cs if i > 0 else 0))
            res = max(cs, (res := x if i == 0 else res))
        return res
"""

    def _passed_results(self):
        return [
            RunnerResult(0, "Passed", "698", runtime_ms=5.0,
                         expected_output="698", actual_output="698"),
            RunnerResult(1, "Passed", "6", runtime_ms=3.0,
                         expected_output="6", actual_output="6"),
        ]

    def test_forced_ac_overrides_llm_wa(self):
        """LLM 即便判 WA，forced_verdict=AC 也必须让最终 verdict=AC。"""
        mock_structured = MagicMock()
        # LLM 错误地判了 WA（正是本次 bug 的表现）
        mock_structured.invoke.return_value = JudgeAnalysis(
            verdict="WA",
            warm_feedback="这段代码 WA，初始化有陷阱…",
            repair_suggestion="把 cs 改成标准 Kadane 形式",
            should_retry=True,
        )
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        with patch("code_tutor_agent.agents.agent_judge.get_llm", return_value=mock_llm):
            analysis = analyze_judge_results(
                code=self.CORRECT_CODE,
                title="最大子数组和",
                difficulty="medium",
                topic="数组",
                description="返回最大子数组和",
                results=self._passed_results(),
                forced_verdict="AC",
            )

        assert analysis.verdict == "AC"
        assert analysis.should_retry is False

    def test_forced_wa_overrides_llm_ac(self):
        """反之，客观结果 WA 时，LLM 若判 AC 也必须被纠正为 WA。"""
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = JudgeAnalysis(
            verdict="AC", warm_feedback="全过", repair_suggestion="", should_retry=False,
        )
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        results = [
            RunnerResult(0, "Passed", "698", expected_output="698", actual_output="698"),
            RunnerResult(1, "Wrong Answer", "expected=6 got=5",
                         expected_output="6", actual_output="5"),
        ]
        with patch("code_tutor_agent.agents.agent_judge.get_llm", return_value=mock_llm):
            analysis = analyze_judge_results(
                code=self.CORRECT_CODE, title="t", difficulty="easy", topic="数组",
                description="", results=results, forced_verdict="WA",
            )

        assert analysis.verdict == "WA"
        assert analysis.should_retry is True


class TestDeterministicVerdict:
    """_deterministic_verdict 直接归约执行引擎客观结果。"""

    def test_all_passed(self):
        res = [RunnerResult(0, "Passed", ""), RunnerResult(1, "Passed", "")]
        assert _deterministic_verdict(res) == "AC"

    def test_wrong_answer(self):
        res = [RunnerResult(0, "Passed", ""), RunnerResult(1, "Wrong Answer", "")]
        assert _deterministic_verdict(res) == "WA"

    def test_runtime_error(self):
        res = [RunnerResult(0, "Runtime Error", "")]
        assert _deterministic_verdict(res) == "RE"

    def test_tle(self):
        res = [RunnerResult(0, "TLE", "")]
        assert _deterministic_verdict(res) == "TLE"

    def test_skipped_excluded(self):
        # 全部 Skipped（无参考答案）→ 视为通过，绝不误判 WA
        res = [RunnerResult(0, "Skipped", ""), RunnerResult(1, "Skipped", "")]
        assert _deterministic_verdict(res) == "AC"

    def test_passed_with_skipped_is_ac(self):
        res = [RunnerResult(0, "Passed", ""), RunnerResult(1, "Skipped", "")]
        assert _deterministic_verdict(res) == "AC"


class TestFormatShowsActual:
    """format_results_for_prompt 必须透出每个用例的期望/实际输出，避免 LLM 臆造。"""

    def test_passed_case_shows_expected_and_actual(self):
        results = [RunnerResult(0, "Passed", "698",
                                expected_output="698", actual_output="698")]
        text = format_results_for_prompt(results)
        assert "期望输出='698'" in text
        assert "实际输出='698'" in text

    def test_failed_case_shows_expected_and_actual(self):
        results = [RunnerResult(0, "Wrong Answer", "expected=698 got=37",
                                expected_output="698", actual_output="37")]
        text = format_results_for_prompt(results)
        assert "期望输出='698'" in text
        assert "实际输出='37'" in text
