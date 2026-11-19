"""修复工具箱单元测试（P2，2026-11-19）。

覆盖：工具返回协议、缓存不占配额、配额耗尽、错误分型、同步工具循环。
不调真实 LLM / 不起沙箱进程。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from code_tutor_agent.generation.diagnosis import Defect, Diagnosis
from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.generation.toolbox import ToolBox, run_tool_loop_sync
from code_tutor_agent.sandbox.runner import RunnerResult

_OPTIMAL = (
    "class Solution:\n"
    "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
    "        return [0, 1]\n"
)


def _draft() -> ProblemDraft:
    return ProblemDraft(
        topic="数组",
        difficulty="easy",
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        starter_code="class Solution:\n    def twoSum(self, nums, target):\n        pass\n",
        optimal_solution=_OPTIMAL,
        examples=["Input: nums = [2,7], target = 9\nOutput: [0,1]"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: list[int], target: int -> list[int]",
    )


def _diag() -> Diagnosis:
    return Diagnosis(
        passed=False,
        defects=[Defect(
            code="EXAMPLE_OUTPUT_MISMATCH", severity="blocking",
            where="examples[0]", message="m", allowed_fields=("optimal_solution",),
        )],
        density=0.2,
        fingerprint="fp1",
    )


class _FakeLeetCode:
    def parse_examples(self, examples, starter):
        return [{"input_args": ["[2,7]", "9"], "expected_output": "[0,1]", "explanation": ""}]


class _FakeSandbox:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        self.calls += 1
        return self.batches.pop(0) if self.batches else []

    def random_inputs(self, sig, n, seed, constraints, description):
        return [["[2,7]", "9"]]


def _box(batches, *, max_calls=6):
    agent = SimpleNamespace(leetcode=_FakeLeetCode(), sandbox=_FakeSandbox(batches))
    return ToolBox(agent, _draft(), _diag(), max_calls=max_calls)


def test_run_examples_returns_rows():
    box = _box([[RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[]")]])
    payload = json.loads(box.run_examples(_OPTIMAL))
    assert payload["ok"] is True
    assert payload["tool"] == "run_examples"
    assert payload["cases"][0]["status"] == "Wrong Answer"
    assert payload["cases"][0]["actual"] == "[]"
    assert box._used == 1


def test_cache_hit_does_not_consume_quota():
    box = _box([[RunnerResult(0, "Passed", "[0,1]")]])
    first = box.run_examples(_OPTIMAL)
    second = box.run_examples(_OPTIMAL)
    assert first == second
    assert box._used == 1
    assert box.calls[-1]["cache_hit"] is True


def test_budget_exceeded():
    box = _box(
        [[RunnerResult(0, "Passed", "[0,1]")], [RunnerResult(0, "Passed", "[0,1]")]],
        max_calls=1,
    )
    box.run_examples(_OPTIMAL)
    payload = json.loads(box.run_examples(_OPTIMAL + "\n# changed"))
    assert payload["ok"] is False
    assert payload["error_kind"] == "BUDGET_EXCEEDED"


def test_run_cases_invalid_json():
    payload = json.loads(_box([]).run_cases(_OPTIMAL, "not-json"))
    assert payload["ok"] is False
    assert payload["error_kind"] == "INVALID_ARGS"


def test_run_random_returns_actual_without_stated():
    box = _box([[RunnerResult(0, "Passed", "[0,1]")]])
    payload = json.loads(box.run_random(_OPTIMAL))
    assert payload["ok"] is True
    assert "stated" not in payload["cases"][0]


def test_cross_check_reports_disagreement():
    box = _box([
        [RunnerResult(0, "Passed", "[0,1]")],
        [RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[]")],
    ])
    payload = json.loads(box.cross_check(_OPTIMAL, _OPTIMAL))
    assert payload["agree"] is False
    assert payload["cases"][0]["agree"] is False


def test_lint_reports_truncated_code():
    box = _box([])
    payload = json.loads(box.lint("class Solution:\n    def f(self, x):\n        y = x\n"))
    assert payload["ok"] is True
    assert any("没有任何 return" in i for i in payload["issues"])


# ── 同步工具循环 ────────────────────────────────────────────────


class _FakeToolLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def bind_tools(self, _tools):
        return self

    def invoke(self, _messages):
        return self.responses.pop(0)


def test_tool_loop_runs_tool_then_stops():
    box = _box([[RunnerResult(0, "Passed", "[0,1]")]])
    responses = [
        AIMessage(content="", tool_calls=[
            {"name": "run_examples", "args": {"code": _OPTIMAL}, "id": "t1"},
        ]),
        AIMessage(content="done"),
    ]
    messages, calls = run_tool_loop_sync(
        _FakeToolLLM(responses), [("human", "fix it")], box.tools()
    )
    # human + ai + tool + ai
    assert len(messages) == 4
    assert calls and calls[0]["tool"] == "run_examples"
    assert calls[0]["ok"] is True


def test_tool_loop_stops_on_unknown_tool():
    class _LLM(_FakeToolLLM):
        def invoke(self, _messages):
            return AIMessage(content="", tool_calls=[
                {"name": "save_problem", "args": {}, "id": "t1"},
            ])

    box = _box([])
    messages, calls = run_tool_loop_sync(_LLM([]), [("human", "x")], box.tools())
    assert calls == []
    assert len(messages) == 2  # human + ai，没有执行任何工具
