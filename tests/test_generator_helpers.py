"""generation 包 helper 单元测试（原 nodes/generator 私有 helper 的迁移版）。

覆盖新架构下关键 helper：
* `ProblemGenerationAgent._build_sample_tests` — 解析示例 → 参考解自验证回填
  expected_output（原 `_self_verify_reference` + `_parse_examples_to_test_cases`）；
* `CodeVerifier` — 三查（结构 / 编译 / 无 CoT 泄漏）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.generation import ProblemGenerationAgent  # noqa: E402
from code_tutor_agent.generation.state import ProblemDraft  # noqa: E402
from code_tutor_agent.generation.verifier import CodeVerifier  # noqa: E402

_OPTIMAL = (
    "class Solution:\n"
    "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
    "        return [0, 1]\n"
)


def _draft(**overrides) -> ProblemDraft:
    kwargs = dict(
        topic="数组", difficulty="easy", title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        starter_code=(
            "class Solution:\n"
            "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
            "        pass\n"
        ),
        optimal_solution=_OPTIMAL,
        function_signature="nums: list[int], target: int -> list[int]",
        examples=["输入: nums = [2,7,11,15], target = 9 → 输出: [0, 1]"],
    )
    kwargs.update(overrides)
    return ProblemDraft(**kwargs)


def _run_result(status: str, detail: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, detail=detail)


def _agent_with_run(results) -> ProblemGenerationAgent:
    leetcode = SimpleNamespace(parse_examples=lambda examples, starter: [
        {"input_args": ["[2,7,11,15]", "9"], "expected_output": ""},
    ])
    sandbox = SimpleNamespace(run_solution=lambda *a, **k: results)
    return ProblemGenerationAgent(
        leetcode=leetcode,
        sandbox=sandbox,
        llm=SimpleNamespace(
            generate_problem=lambda *a, **k: None,
            generate_optimal=lambda *a, **k: None,
        ),
        store=SimpleNamespace(save=lambda d: 1),
    )


# ── _build_sample_tests ──
class TestBuildSampleTests:
    def test_all_passed_rewrites_expected(self):
        agent = _agent_with_run([_run_result("Passed", "[0,1]")])
        tcs = agent._build_sample_tests(_draft())
        assert tcs is not None
        assert tcs[0]["expected_output"] == "[0,1]"

    def test_runtime_error_returns_none(self):
        agent = _agent_with_run([_run_result("Runtime Error", "")])
        assert agent._build_sample_tests(_draft()) is None

    def test_tle_returns_none(self):
        agent = _agent_with_run([_run_result("TLE", "")])
        assert agent._build_sample_tests(_draft()) is None

    def test_empty_results_returns_none(self):
        agent = _agent_with_run([])
        assert agent._build_sample_tests(_draft()) is None

    def test_no_actual_output_returns_none(self):
        agent = _agent_with_run([_run_result("Passed", "")])
        assert agent._build_sample_tests(_draft()) is None

    def test_no_reference_solution_returns_none(self):
        agent = _agent_with_run([_run_result("Passed", "[0,1]")])
        d = _draft(optimal_solution="", brute_solution="")
        assert agent._build_sample_tests(d) is None

    def test_no_examples_returns_none(self):
        agent = ProblemGenerationAgent(
            leetcode=SimpleNamespace(parse_examples=lambda examples, starter: []),
            llm=SimpleNamespace(),
            store=SimpleNamespace(),
            sandbox=SimpleNamespace(run_solution=lambda *a, **k: []),
            verifier=CodeVerifier(),  # 不参与该 helper
        )
        d = _draft(examples=[])
        assert agent._build_sample_tests(d) is None


# ── CodeVerifier 三查 ──
class TestCodeVerifier:
    def test_accepts_valid_draft(self):
        ok, issues = CodeVerifier().verify(_draft())
        assert ok
        assert issues == []

    def test_loose_structure(self):
        ok, issues = CodeVerifier().verify(_draft(title="", description="短"))
        assert not ok
        joined = " | ".join(issues)
        assert "title 为空" in joined
        assert "description 缺失或过短" in joined

    def test_starter_must_have_class_and_def(self):
        ok, issues = CodeVerifier().verify(_draft(starter_code="print(1)"))
        assert not ok
        assert any("starter_code" in i for i in issues)

    def test_original_channel_requires_optimal_solution(self):
        ok, issues = CodeVerifier().verify(_draft(optimal_solution="", brute_solution=""))
        assert not ok
        assert "optimal_solution 为空" in issues

    def test_import_channel_skips_optimal_requirement(self):
        ok, issues = CodeVerifier().verify(_draft(optimal_solution="", source_slug="two-sum"))
        assert ok, issues  # 导入题参考解由后台补，不阻断出题

    def test_syntax_error_reported(self):
        ok, issues = CodeVerifier().verify(_draft(optimal_solution="def broken(:"))
        assert not ok
        assert any("语法错误" in i for i in issues)

    def test_cot_leak_in_description_rejected(self):
        ok, issues = CodeVerifier().verify(_draft(description="让我们一步一步来解析这道题"))
        assert not ok
        assert "思维链痕迹" in " | ".join(issues)

    def test_cot_leak_in_solution_comment_rejected(self):
        ok, issues = CodeVerifier().verify(
            _draft(optimal_solution="# 思考过程：先排序再遍历\nclass Solution: pass")
        )
        assert not ok
        assert any("注释含思维链痕迹" in i for i in issues)
