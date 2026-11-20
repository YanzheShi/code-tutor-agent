"""质量闸单元测试（P3，2026-11-19）。

覆盖：区分度、域校验、双解交叉（含锚点）、随机输入上的参考解崩溃，
以及这些缺陷码的分诊结果。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from code_tutor_agent.generation.diagnosis import Defect, Diagnosis, diagnose
from code_tutor_agent.generation.quality import check_quality
from code_tutor_agent.generation.repair import decide
from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.sandbox.runner import RunnerResult


def _draft(**overrides) -> ProblemDraft:
    kwargs = dict(
        topic="数组",
        difficulty="easy",
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        starter_code="class Solution:\n    def solve(self, nums, target):\n        pass\n",
        optimal_solution="OPT",
        brute_solution="BRUTE",
        examples=["Input: a\nOutput: 1", "Input: b\nOutput: 2"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: list[int], target: int -> list[int]",
    )
    kwargs.update(overrides)
    return ProblemDraft(**kwargs)


_CASES = [
    {"input_args": ["[2,7]", "9"], "expected_output": "[0,1]", "explanation": ""},
    {"input_args": ["[3,2]", "5"], "expected_output": "[1,0]", "explanation": ""},
]


class _QueueSandbox:
    def __init__(self, batches, *, domain_ok=True, inputs=None):
        self.batches = list(batches)
        self.domain_ok = domain_ok
        self.inputs = inputs if inputs is not None else [["[2,7]", "9"]]

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        return self.batches.pop(0) if self.batches else []

    def random_inputs(self, sig, n, seed, constraints, description):
        return self.inputs

    def input_in_domain(self, sig, args, char_domain=None):
        return self.domain_ok


class _LeetCode:
    def __init__(self, cases=None):
        self.cases = cases if cases is not None else _CASES

    def parse_examples(self, examples, starter):
        return self.cases


def _agent(sandbox, cases=None):
    return SimpleNamespace(sandbox=sandbox, leetcode=_LeetCode(cases))


def _passed(value: str) -> RunnerResult:
    return RunnerResult(0, "Passed", value)


def test_weak_examples_detected():
    sandbox = _QueueSandbox([])
    results = [_passed("0"), _passed("0")]
    defects, anchor = check_quality(_agent(sandbox), _draft(), _CASES, results)
    assert [d.code for d in defects] == ["WEAK_EXAMPLES"]
    assert anchor is False


def test_domain_violation_detected():
    sandbox = _QueueSandbox([], domain_ok=False)
    defects, _ = check_quality(_agent(sandbox), _draft(), _CASES, [_passed("1"), _passed("2")])
    assert defects and defects[0].code == "EXAMPLE_INPUT_OUT_OF_DOMAIN"


def test_solver_disagreement_reported_when_repeated():
    sandbox = _QueueSandbox(
        [
            [RunnerResult(0, "Skipped", "1"), RunnerResult(0, "Skipped", "1")],
            [RunnerResult(0, "Skipped", "2"), RunnerResult(0, "Skipped", "2")],
        ],
        inputs=[["[2,7]", "9"], ["[3,2]", "5"]],
    )
    results = [_passed("1"), _passed("2")]
    defects, anchor = check_quality(_agent(sandbox), _draft(), _CASES, results)
    assert defects and all(d.code == "SOLVER_DISAGREEMENT" for d in defects)
    assert len(defects) == 2
    assert defects[0].allowed_fields == ("optimal_solution", "brute_solution")
    assert anchor is False


def test_single_disagreement_is_ignored():
    """单条分歧可能只是随机输入越界，不阻断。"""
    sandbox = _QueueSandbox(
        [
            [RunnerResult(0, "Skipped", "1")],
            [RunnerResult(0, "Skipped", "2")],
        ],
        inputs=[["[2,7]", "9"]],
    )
    results = [_passed("1"), _passed("2")]
    defects, _ = check_quality(_agent(sandbox), _draft(), _CASES, results)
    assert defects == []


def test_type_mismatch_is_ignored_as_bad_brute():
    """brute 返回 bool、optimal 返回 int → 是 brute 写坏了，不算真分歧。"""
    sandbox = _QueueSandbox(
        [
            [RunnerResult(0, "Skipped", "0"), RunnerResult(0, "Skipped", "0")],
            [RunnerResult(0, "Skipped", "False"), RunnerResult(0, "Skipped", "False")],
        ],
        inputs=[["[2,7]", "9"], ["[3,2]", "5"]],
    )
    results = [_passed("1"), _passed("2")]
    defects, _ = check_quality(_agent(sandbox), _draft(), _CASES, results)
    assert defects == []


def test_reference_crash_on_random_input_is_warning_only():
    sandbox = _QueueSandbox([
        [RunnerResult(0, "Runtime Error", "IndexError: list index out of range")],
        [RunnerResult(0, "Skipped", "1")],
    ])
    results = [_passed("1"), _passed("2")]
    defects, _ = check_quality(_agent(sandbox), _draft(), _CASES, results)
    assert defects[0].code == "REFERENCE_RE"
    assert defects[0].severity == "quality"


def test_no_brute_skips_cross_check():
    sandbox = _QueueSandbox([])
    defects, anchor = check_quality(
        _agent(sandbox), _draft(brute_solution=""), _CASES, [_passed("1"), _passed("2")]
    )
    assert defects == []
    assert anchor is False


def test_no_random_generator_skips_cross_check():
    sandbox = SimpleNamespace(run_batch=lambda *a, **k: [], input_in_domain=lambda *a, **k: True)
    defects, _ = check_quality(_agent(sandbox), _draft(), _CASES, [_passed("1"), _passed("2")])
    assert defects == []


# ── diagnose 集成：示例全过但质量闸拒绝 ────────────────────────


class _DiagSandbox:
    def __init__(self, *, domain_ok=True):
        self.domain_ok = domain_ok
        self.calls = 0

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        self.calls += 1
        return [RunnerResult(i, "Passed", "0") for i in range(len(cases))]

    def input_in_domain(self, sig, args, char_domain=None):
        return self.domain_ok

    def random_inputs(self, sig, n, seed, constraints, description):
        return []


_VALID = (
    "class Solution:\n"
    "    def solve(self, nums, target):\n"
    "        return 0\n"
)


def test_diagnose_marks_weak_examples_as_quality_stage():
    agent = _agent(_DiagSandbox(), cases=_CASES)
    diag = diagnose(_draft(optimal_solution=_VALID, brute_solution=_VALID), agent)
    assert not diag.passed
    assert diag.stage_reached == "quality"
    assert "WEAK_EXAMPLES" in diag.codes()


def test_diagnose_marks_domain_violation():
    agent = _agent(_DiagSandbox(domain_ok=False), cases=_CASES)
    diag = diagnose(_draft(optimal_solution=_VALID, brute_solution=_VALID), agent)
    assert not diag.passed
    assert diag.stage_reached == "quality"
    assert "EXAMPLE_INPUT_OUT_OF_DOMAIN" in diag.codes()


class _WarnSandbox(_DiagSandbox):
    """示例全过，但随机输入上参考解崩溃 → 只应产生 warning。"""

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        if cases and cases[0].get("expected_output", "") == "":
            return [RunnerResult(0, "Runtime Error", "IndexError: list index out of range")
                    for _ in cases]
        return [RunnerResult(i, "Passed", str(i + 1)) for i in range(len(cases))]

    def random_inputs(self, sig, n, seed, constraints, description):
        return [["[1]"]]


def test_diagnose_passes_with_quality_warning():
    agent = _agent(_WarnSandbox(), cases=_CASES)
    diag = diagnose(_draft(optimal_solution=_VALID, brute_solution=_VALID), agent)
    assert diag.passed is True
    assert diag.stage_reached == "quality"
    assert "REFERENCE_RE" in diag.codes()


# ── 分诊 ────────────────────────────────────────────────────────


def _diag(codes, *, anchor=False, density=0.2, passed=False):
    defects = [
        Defect(code=c, severity="blocking", where="examples", message="m")
        for c in codes
    ]
    return Diagnosis(
        passed=passed, defects=defects, anchor_available=anchor,
        density=density, fingerprint="fp",
    )


def test_decide_quality_codes_resample():
    assert decide(_diag(["WEAK_EXAMPLES"]), _draft(), 0) == "resample"
    assert decide(_diag(["EXAMPLE_INPUT_OUT_OF_DOMAIN"]), _draft(), 0) == "resample"


def test_decide_solver_disagreement_repairs():
    assert decide(_diag(["SOLVER_DISAGREEMENT"]), _draft(), 0) == "repair"
