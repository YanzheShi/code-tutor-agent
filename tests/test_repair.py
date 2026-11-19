"""修复回路单元测试（P1，2026-11-19）。

覆盖：分诊、反降级闸、有界循环（轮数 / 无进展 / 指纹 / deadline / LLM 空返回）。
不调真实 LLM、不跑沙箱。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from code_tutor_agent.generation.diagnosis import Defect, Diagnosis
from code_tutor_agent.generation.repair import (
    RepairBudget,
    RepairSession,
    anti_degradation,
    decide,
)
from code_tutor_agent.generation.state import ProblemDraft, RepairRevision

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
        examples=[
            "Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]",
            "Input: nums = [3,2,4], target = 6\nOutput: [1,2]",
        ],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: list[int], target: int -> list[int]",
    )
    kwargs.update(overrides)
    return ProblemDraft(**kwargs)


def _defect(code="EXAMPLE_OUTPUT_MISMATCH", severity="blocking", allowed=("optimal_solution",)):
    return Defect(
        code=code, severity=severity, where="examples[0]", message="m",
        allowed_fields=allowed,
    )


def _diag(codes=("EXAMPLE_OUTPUT_MISMATCH",), passed=False, anchor=False,
          density=0.2, env=False, fingerprint="fp1"):
    defects = []
    for code in codes:
        severity = {"COT_IN_CODE": "cosmetic", "CODE_TRUNCATED": "fatal"}.get(code, "blocking")
        defects.append(_defect(code, severity, allowed=("optimal_solution",)))
    return Diagnosis(
        passed=passed, defects=defects, env_failure=env,
        anchor_available=anchor, density=density, fingerprint=fingerprint,
    )


# ── 分诊 ────────────────────────────────────────────────────────


def test_decide_passed_saves():
    assert decide(_diag(passed=True, codes=()), _draft(), 0) == "save"


def test_decide_env_falls_back():
    assert decide(_diag(env=True), _draft(), 0) == "fallback"


def test_decide_truncated_resamples():
    assert decide(_diag(codes=("CODE_TRUNCATED",), density=1.0), _draft(), 0) == "resample"


def test_decide_cosmetic_cleans():
    assert decide(_diag(codes=("COT_IN_CODE",), density=0.0), _draft(), 0) == "clean"


def test_decide_high_density_resamples():
    assert decide(_diag(codes=("A", "B", "C"), density=0.7), _draft(), 0) == "resample"


def test_decide_anchor_repairs():
    assert decide(_diag(codes=("A", "B", "C"), density=0.2, anchor=True), _draft(), 0) == "repair"


def test_decide_two_blocking_repairs():
    assert decide(_diag(codes=("A", "B")), _draft(), 0) == "repair"


# ── 反降级闸 ────────────────────────────────────────────────────


def test_anti_degradation_accepts_optimal_change():
    old = _draft()
    new = _draft(optimal_solution=_OPTIMAL.replace("seen = {}", "seen = dict()"))
    assert anti_degradation(old, new, defects=[_defect()]) == []


def test_anti_degradation_rejects_title_change():
    old, new = _draft(), _draft(title="Two Sum II")
    violations = anti_degradation(old, new, defects=[_defect()])
    assert any("title" in v for v in violations)


def test_anti_degradation_allows_title_for_duplicate():
    old, new = _draft(), _draft(title="Two Sum II")
    assert anti_degradation(old, new, defects=[_defect(code="TITLE_DUPLICATE")]) == []


def test_anti_degradation_rejects_example_removal():
    old = _draft()
    new = _draft(examples=old.examples[:1])
    assert any("示例数减少" in v for v in anti_degradation(old, new, defects=[_defect()]))


def test_anti_degradation_rejects_loosened_constraints():
    old = _draft()
    new = _draft(constraints=["1 <= nums.length <= 10^5"])
    assert any("长度约束被放宽" in v for v in anti_degradation(old, new, defects=[_defect()]))


def test_anti_degradation_rejects_unauthorized_field():
    old = _draft()
    new = _draft(description="A much longer but completely different description of the task.")
    violations = anti_degradation(old, new, defects=[_defect(allowed=("optimal_solution",))])
    assert any("未授权字段" in v for v in violations)


def test_anti_degradation_rejects_degenerate_examples():
    old = _draft()
    new = _draft(examples=[
        "Input: a\nOutput: 0",
        "Input: b\nOutput: 0",
    ])
    assert any("退化解" in v for v in anti_degradation(old, new, defects=[_defect()]))


# ── 有界循环 ────────────────────────────────────────────────────


class _FakeAgent:
    def __init__(self, revisions, diagnoses):
        self._revisions = list(revisions)
        self._diagnoses = list(diagnoses)
        self.llm = SimpleNamespace(repair_problem=self._repair)
        self.repair_calls = 0
        self.diagnose_calls = 0

    def _repair(self, draft, diagnosis, **_kwargs):
        self.repair_calls += 1
        return self._revisions.pop(0) if self._revisions else None

    def diagnose(self, draft):
        self.diagnose_calls += 1
        return self._diagnoses.pop(0) if self._diagnoses else _diag(passed=True)


def _budget(rounds=2, deadline_s=60):
    return RepairBudget(max_rounds=rounds, total_deadline_ts=time.time() + deadline_s)


def _revision(**overrides):
    return RepairRevision(draft=_draft(**overrides), change_summary="fix", confidence=0.8)


def test_session_repairs_on_first_round():
    agent = _FakeAgent([_revision()], [_diag(passed=True, codes=())])
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert out.ok and out.reason == "repaired"
    assert len(out.rounds) == 1 and out.rounds[0].outcome == "passed"


def test_session_stops_when_no_progress():
    # 两轮都是同样的 blocking 分 → 连续 2 次无进展
    agent = _FakeAgent(
        [_revision(), _revision()],
        [_diag(fingerprint="fp2"), _diag(fingerprint="fp3")],
    )
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert not out.ok and out.reason == "no_progress"
    assert agent.repair_calls == 2


def test_session_stops_on_fingerprint_repeat():
    agent = _FakeAgent([_revision()], [_diag(fingerprint="fp1")])
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag(fingerprint="fp1")
    )
    assert not out.ok and out.reason == "fingerprint"


def test_session_stops_on_deadline_without_calling_llm():
    agent = _FakeAgent([], [])
    out = RepairSession(agent, _budget(deadline_s=-1), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert not out.ok and out.reason == "deadline"
    assert agent.repair_calls == 0


def test_session_rejects_degraded_revision():
    # 约束被放宽不可回滚 → 整轮拒绝
    agent = _FakeAgent([_revision(constraints=["1 <= nums.length <= 10^5"])], [])
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert not out.ok and out.reason == "rejected_degraded"
    assert agent.diagnose_calls == 0


def test_session_reverts_readonly_fields_instead_of_rejecting():
    """模型顺手改 examples/title：回滚后继续判定，而不是整轮报销。"""
    agent = _FakeAgent(
        [_revision(title="Changed", examples=["hacked"])],
        [_diag(passed=True, codes=())],
    )
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert out.ok and out.reason == "repaired"
    assert out.rounds[0].reverted == ["examples", "title"]
    assert out.draft.title == "Two Sum"
    assert out.draft.examples == _draft().examples


def test_session_handles_empty_repair_result():
    agent = _FakeAgent([], [])
    out = RepairSession(agent, _budget(), SimpleNamespace(event=lambda ev: None)).run(
        _draft(), _diag()
    )
    assert not out.ok and out.reason == "llm_error"


# ── LlmGateway.repair_problem（结构化提交）─────────────────────


from code_tutor_agent import config as cfg  # noqa: E402
from code_tutor_agent.generation.gateways.llm import LlmGateway, ProblemRevision  # noqa: E402
from code_tutor_agent.models.problem import Problem  # noqa: E402


class _Structured:
    def __init__(self, obj):
        self.obj = obj

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def invoke(self, *_a, **_k):
        return self.obj


class _FakeRepairLLM:
    def __init__(self, obj=None, error=None):
        self.obj = obj
        self.error = error
        self.schema = None

    def with_structured_output(self, schema):
        self.schema = schema
        if self.error:
            raise self.error
        return _Structured(self.obj)


def _revision_problem() -> Problem:
    return Problem(
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        difficulty="easy",
        topic="数组",
        examples=["Input: nums = [2,7], target = 9\nOutput: [0,1]"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="nums: List[int], target: int -> List[int]",
        starter_code="class Solution:\n    def twoSum(self, nums, target):\n        pass\n",
        optimal_solution=_OPTIMAL,
    )


def test_gateway_repair_returns_revision(monkeypatch):
    revision = ProblemRevision(
        problem=_revision_problem(),
        change_summary="修了边界",
        addressed_defects=["EXAMPLE_OUTPUT_MISMATCH"],
        confidence=0.9,
    )
    fake = _FakeRepairLLM(revision)
    monkeypatch.setattr(cfg, "get_llm", lambda *a, **k: fake)

    out = LlmGateway().repair_problem(_draft(), _diag())
    assert out is not None
    assert out.draft.title == "Two Sum"
    assert out.change_summary == "修了边界"
    assert out.addressed_defects == ["EXAMPLE_OUTPUT_MISMATCH"]
    assert out.tool_calls == []
    assert fake.schema is ProblemRevision


def test_gateway_repair_returns_none_on_error(monkeypatch):
    fake = _FakeRepairLLM(error=RuntimeError("gateway 500"))
    monkeypatch.setattr(cfg, "get_llm", lambda *a, **k: fake)
    assert LlmGateway().repair_problem(_draft(), _diag()) is None


# ── run() 集成：失败 → 修复 → 落库 ──────────────────────────────

from code_tutor_agent.generation import ProblemGenerationAgent  # noqa: E402
from code_tutor_agent.generation.state import GenerationContext  # noqa: E402
from code_tutor_agent.sandbox.runner import RunnerResult  # noqa: E402


class _FlowLeetCode:
    def parse_examples(self, examples, starter):
        return [{"input_args": ["[2,7]", "9"], "expected_output": "[0,1]", "explanation": ""}]


class _FlowSandbox:
    def __init__(self):
        self.calls = 0

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        self.calls += 1
        if self.calls == 1:
            return [RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[]")]
        return [RunnerResult(0, "Passed", "[0,1]")]

    def run_solution(self, code, cases, function_signature=""):
        return self.run_batch(code, cases, function_signature)


class _FlowStore:
    def __init__(self):
        self.saved = []

    def save(self, draft):
        self.saved.append(draft)
        return 1, False


class _FlowLLM:
    def __init__(self):
        self.repairs = 0

    def generate_problem(self, *_a, **_k):
        return _draft()

    def generate_optimal(self, *_a, **_k):
        return ""

    def generate_dual(self, *_a, **_k):
        return None

    def repair_problem(self, draft, diagnosis, **_kwargs):
        self.repairs += 1
        return RepairRevision(
            draft=_draft(optimal_solution=_OPTIMAL.replace("return []", "return [0, 1]")),
            change_summary="修了返回",
            addressed_defects=["EXAMPLE_OUTPUT_MISMATCH"],
            confidence=0.9,
        )


def test_run_repairs_then_saves():
    store = _FlowStore()
    llm = _FlowLLM()
    agent = ProblemGenerationAgent(
        leetcode=_FlowLeetCode(),
        llm=llm,
        store=store,
        sandbox=_FlowSandbox(),
    )
    result = agent.run(GenerationContext(topic="数组", difficulty="easy"))
    assert result.ok, result.error
    assert result.channel == "llm"
    assert llm.repairs == 1
    assert len(store.saved) == 1
    assert "return [0, 1]" in store.saved[0].optimal_solution
    assert store.saved[0].test_cases[0]["expected_output"] == "[0,1]"
