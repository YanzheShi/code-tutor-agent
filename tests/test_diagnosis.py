"""诊断层与 trace 的单元测试（P0，2026-11-19）。

覆盖：
- ``diagnose`` 的静态 / 沙箱 / ENV / 锚点 / 解析失败分支；
- ``agent_problem.generate_problem`` 的「剥 CoT 注释后直接通过」零 LLM 修复；
- ``trace.record_gen_trace`` 的 JSONL 落盘。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_tutor_agent.agents.agent_problem as agent_problem
from code_tutor_agent.generation.diagnosis import diagnose
from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.generation.trace import record_gen_trace
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.sandbox.runner import RunnerResult

_OPTIMAL = (
    "class Solution:\n"
    "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
    "        seen = {}\n"
    "        for i, n in enumerate(nums):\n"
    "            if target - n in seen:\n"
    "                return [seen[target - n], i]\n"
    "            seen[n] = i\n"
    "        return []\n"
)
_BRUTE = (
    "class Solution:\n"
    "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
    "        for i in range(len(nums)):\n"
    "            for j in range(i + 1, len(nums)):\n"
    "                if nums[i] + nums[j] == target:\n"
    "                    return [i, j]\n"
    "        return []\n"
)


def _draft(**overrides) -> ProblemDraft:
    kwargs = dict(
        topic="数组",
        difficulty="easy",
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        starter_code=(
            "class Solution:\n"
            "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
            "        pass\n"
        ),
        optimal_solution=_OPTIMAL,
        brute_solution=_BRUTE,
        examples=["Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: list[int], target: int -> list[int]",
    )
    kwargs.update(overrides)
    return ProblemDraft(**kwargs)


_TC0 = {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]", "explanation": ""}
_TC1 = {"input_args": ["[3,2,4]", "6"], "expected_output": "[1,2]", "explanation": ""}


class _FakeLeetCode:
    def __init__(self, cases):
        self.cases = cases

    def parse_examples(self, examples, starter):
        return self.cases


class _SeqSandbox:
    """按调用顺序返回预设批次；记录调用次数，用于断言「静态失败不跑沙箱」。"""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


def _agent(cases, batches, *, optimal=_OPTIMAL, brute=_BRUTE):
    return SimpleNamespace(
        leetcode=_FakeLeetCode(cases),
        sandbox=_SeqSandbox(batches),
    )


# ── diagnose ────────────────────────────────────────────────────


def test_diagnose_passes_and_backfills_expected():
    agent = _agent([_TC0], [[RunnerResult(0, "Passed", "[0,1]")]])
    diag = diagnose(_draft(), agent)
    assert diag.passed
    assert diag.verified_cases[0]["expected_output"] == "[0,1]"
    assert diag.defects == []
    assert diag.density == 0.0


def test_diagnose_partial_mismatch_forbids_editing_examples():
    """部分示例不一致也不许改示例：防止「改考卷迎合错解」。"""
    agent = _agent(
        [_TC0, _TC1],
        [
            [RunnerResult(0, "Passed", "[0,1]"), RunnerResult(1, "Wrong Answer", "expected=[1,2] got=[0,1]")],
            [RunnerResult(0, "Passed", "[0,1]"), RunnerResult(1, "Passed", "[1,2]")],
        ],
    )
    diag = diagnose(_draft(examples=["e0", "e1"]), agent)
    assert not diag.passed
    assert diag.anchor_available is True
    d = diag.defects[0]
    assert d.code == "EXAMPLE_OUTPUT_MISMATCH"
    assert d.where == "examples[1]"
    assert d.stated == "[1,2]"
    assert d.actual == "[0,1]"
    assert d.allowed_fields == ("optimal_solution", "description")
    assert "examples" not in d.allowed_fields


def test_diagnose_all_mismatch_allows_optimal_and_description():
    agent = _agent(
        [_TC0],
        [
            [RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[]")],
            [RunnerResult(0, "Passed", "[0,1]")],
        ],
    )
    diag = diagnose(_draft(), agent)
    assert diag.anchor_available is True
    assert diag.defects[0].allowed_fields == ("optimal_solution", "description")


def test_diagnose_env_failure_short_circuits():
    agent = _agent(
        [_TC0],
        [[RunnerResult(0, "Judge Error", "Judge0 unavailable (local fallback disabled)")]],
    )
    diag = diagnose(_draft(), agent)
    assert not diag.passed
    assert diag.env_failure is True
    assert diag.codes() == ["ENV_UNAVAILABLE"]
    assert agent.sandbox.calls == 1  # 不做锚点检查


def test_diagnose_static_failure_skips_sandbox():
    agent = _agent([_TC0], [])
    diag = diagnose(_draft(title=""), agent)
    assert not diag.passed
    assert diag.stage_reached == "static"
    assert diag.codes() == ["STRUCT_MISSING_FIELD"]
    assert agent.sandbox.calls == 0


def test_diagnose_truncated_is_fatal():
    truncated = (
        "class Solution:\n"
        "    def solve(self, nums: list[int]) -> int:\n"
        "        total = 0\n"
    )
    agent = _agent([_TC0], [])
    diag = diagnose(_draft(optimal_solution=truncated), agent)
    assert not diag.passed
    assert "CODE_TRUNCATED" in diag.codes()
    assert diag.density == 1.0
    assert agent.sandbox.calls == 0


def test_diagnose_example_parse_failure():
    agent = _agent([], [])
    diag = diagnose(_draft(), agent)
    assert not diag.passed
    assert diag.stage_reached == "examples"
    assert diag.codes() == ["EXAMPLE_PARSE_FAIL"]
    assert agent.sandbox.calls == 0


# ── 零 LLM 修复：剥注释后通过 ─────────────────────────────────


class _StructuredOutput:
    """模拟 ``prompt | llm.with_structured_output(...)`` 的 Runnable。

    langchain 的 coerce_to_runnable 要求对象可被 ``|`` 组合，故必须实现
    ``__or__`` / ``__ror__``（与 tests/test_problem_agent.py 的 fake 同款）。
    """

    def __init__(self, obj):
        self.obj = obj

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def __call__(self, *_args, **_kwargs):
        return self.obj

    def invoke(self, *_args, **_kwargs):
        return self.obj


class _FakeLLM:
    def __init__(self, obj):
        self.obj = obj

    def with_structured_output(self, _schema):
        return _StructuredOutput(self.obj)


def _problem_with_cot_comment() -> Problem:
    return Problem(
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        difficulty="easy",
        topic="数组",
        examples=["Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: List[int], target: int -> List[int]",
        starter_code=(
            "class Solution:\n"
            "    def twoSum(self, nums: List[int], target: int) -> List[int]:\n"
            "        pass\n"
        ),
        optimal_solution=(
            "class Solution:\n"
            "    def twoSum(self, nums: List[int], target: int) -> List[int]:\n"
            "        # Wait, let me think about the hash map\n"
            "        seen = {}\n"
            "        for i, n in enumerate(nums):\n"
            "            if target - n in seen:\n"
            "                return [seen[target - n], i]\n"
            "            seen[n] = i\n"
            "        return []\n"
        ),
        brute_solution="",
    )


def test_generate_problem_strips_cot_comments_and_accepts(monkeypatch):
    monkeypatch.setattr(
        agent_problem, "get_llm", lambda *a, **k: _FakeLLM(_problem_with_cot_comment())
    )
    out = agent_problem.generate_problem("数组", "easy", max_retries=1)
    assert "Wait" not in out.optimal_solution
    assert "seen = {}" in out.optimal_solution
    assert out.title == "Two Sum"


def test_generate_problem_still_rejects_truncated_after_strip(monkeypatch):
    p = _problem_with_cot_comment()
    p.optimal_solution = (
        "class Solution:\n"
        "    def twoSum(self, nums: List[int], target: int) -> List[int]:\n"
        "        # Wait, I need to think again\n"
        "        seen = {}\n"
    )
    monkeypatch.setattr(agent_problem, "get_llm", lambda *a, **k: _FakeLLM(p))
    with pytest.raises(RuntimeError):
        agent_problem.generate_problem("数组", "easy", max_retries=1)


# ── trace ───────────────────────────────────────────────────────


def test_record_trace_writes_jsonl(tmp_path: Path, monkeypatch):
    path = tmp_path / "gen.jsonl"
    monkeypatch.setenv("CTA_GEN_TRACE_PATH", str(path))
    record_gen_trace({"session_id": "s1", "stage": "examples", "codes": ["EXAMPLE_OUTPUT_MISMATCH"]})
    record_gen_trace({"session_id": "s2", "stage": "static", "codes": ["COT_IN_CODE"]})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["session_id"] == "s1"
    assert first["codes"] == ["EXAMPLE_OUTPUT_MISMATCH"]
    assert "ts" in first
