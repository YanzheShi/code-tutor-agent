"""出题 Agent（Problem Agent）专属测试套件 —— 不依赖真实 LLM / DB / skill-engine。

运行：
    uv run pytest tests/test_problem_agent.py -q

覆盖：
* 主通道 ``generate_problem``（LLM 结构化输出、max_tokens 限流、重试、全失败抛 RuntimeError）
* ``verify_problem`` 自校验（编译 / 思维链 / starter_code 推导）
* ``_extract_code`` 围栏剥离
* 出题不依赖外部工具：``ProblemAgent.generate`` 降级链仅 LLM → 静态兜底
* ``generate_detailed_solution``（题解，仍走 skill-engine，属另一功能）
* 统一入口 ``ProblemAgent.generate`` 的降级链（LLM → 静态兜底）
"""

from __future__ import annotations

import pytest

from code_tutor_agent.agents import agent_problem as agent_problem
from code_tutor_agent.agents.agent_problem import (
    GenerationOutcome,
    ProblemAgent,
    ProblemChannel,
    _extract_code,
    _flat_to_problem,
    generate_detailed_solution,
    generate_problem,
    verify_problem,
)
from code_tutor_agent.models.problem import Problem


# ───────────────────────── 公共 mock 工具 ─────────────────────────

class _StructuredOutput:
    """模拟 ``llm.with_structured_output(Problem)`` 的返回值。

    必须可调用（callable），否则 ``prompt | structured_llm`` 在
    langchain 的 coerce_to_runnable 阶段会因类型不被支持而抛 TypeError。
    """

    def __init__(self, ret):
        self._ret = ret

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def __call__(self, *args, **kwargs):
        if getattr(self._ret, "_is_boom", False):
            raise RuntimeError("boom")
        return self._ret

    def invoke(self, *args, **kwargs):
        return self(*args, **kwargs)


class _FailingStructured:
    """invoke 始终失败，用于模拟 LLM 结构化输出异常。"""

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def __call__(self, *args, **kwargs):
        raise RuntimeError("truncated at 16384 tokens")

    def invoke(self, *args, **kwargs):
        raise RuntimeError("truncated at 16384 tokens")


class _FakeLLM:
    def __init__(self, ret):
        self._ret = ret

    def with_structured_output(self, _schema):
        return _StructuredOutput(self._ret)


def _boom(topic, difficulty, purpose="problem", max_retries=1):
    raise RuntimeError("boom")


_boom._is_boom = True  # type: ignore[attr-defined]


def _valid_problem() -> Problem:
    return Problem(
        title="Two Sum",
        description="Given an array ...",
        difficulty="easy",
        topic="数组",
        examples=["Input: nums=[2,7,11,15], target=9 -> [0,1]"],
        constraints=["1 <= nums.length <= 10^4"],
        function_signature="twoSum: List[int], int -> List[int]",
        starter_code="class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        pass\n",
        optimal_solution=(
            "class Solution:\n"
            "    def twoSum(self, nums: List[int], target: int) -> List[int]:\n"
            "        seen = {}\n"
            "        for i, n in enumerate(nums):\n"
            "            if target - n in seen:\n"
            "                return [seen[target - n], i]\n"
            "            seen[n] = i\n"
            "        return []\n"
        ),
        brute_solution=(
            "class Solution:\n"
            "    def twoSum(self, nums: List[int], target: int) -> List[int]:\n"
            "        for i in range(len(nums)):\n"
            "            for j in range(i + 1, len(nums)):\n"
            "                if nums[i] + nums[j] == target:\n"
            "                    return [i, j]\n"
            "        return []\n"
        ),
    )


def _stub_problem() -> Problem:
    """模拟模型吐出的空题：标题/难度/示例/约束都填了，但 optimal_solution 只是桩。"""
    return Problem(
        title="Climbing Stairs",
        description="You are climbing a staircase with n steps. Each time you can either climb 1 or 2 steps. In how many distinct ways can you climb to the top?",
        difficulty="medium",
        topic="动态规划",
        examples=["Input: n=2 -> 2"],
        constraints=["1 <= n <= 45"],
        function_signature="climbStairs: int -> int",
        starter_code="class Solution:\n    def climbStairs(self, n: int) -> int:\n        pass\n",
        optimal_solution="class Solution:\n    def climbStairs(self, n: int) -> int:\n        pass\n",
    )


# ───────────────────────── 主通道：generate_problem ─────────────────────────

from unittest.mock import MagicMock


def test_generate_problem_llm_success(monkeypatch):
    p = _valid_problem()
    monkeypatch.setattr(agent_problem, "get_llm", lambda *a, **k: _FakeLLM(p))
    out = generate_problem("数组", "easy")
    assert isinstance(out, Problem)
    assert out.title == "Two Sum"


def test_generate_problem_passes_correct_purpose(monkeypatch):
    captured: dict = {}

    def _get_llm(*_a, **kw):
        captured.update(kw)
        failing = MagicMock()
        failing.with_structured_output.return_value = _FailingStructured()
        return failing

    monkeypatch.setattr(agent_problem, "get_llm", _get_llm)
    with pytest.raises(RuntimeError):
        generate_problem("数组", "easy", max_retries=0)
    assert captured.get("purpose") == "problem"


def test_generate_problem_all_llm_failures_raises_runtimeerror(monkeypatch):
    """Bug7：全部 LLM 调用失败时抛 RuntimeError（供上层降级），而非 UnboundLocalError。"""
    failing_llm = MagicMock()
    failing_llm.with_structured_output.return_value = _FailingStructured()
    monkeypatch.setattr(agent_problem, "get_llm", lambda *a, **k: failing_llm)
    with pytest.raises(RuntimeError):
        generate_problem("数组", "easy", max_retries=0)


def test_generate_problem_retries_then_raises_on_stub(monkeypatch):
    """空题目（桩解）应触发重试，重试耗尽后抛 RuntimeError 让上层降级，而非返回空题。"""
    calls = {"n": 0}

    class _CountingStructured(_StructuredOutput):
        def __call__(self, *args, **kwargs):
            calls["n"] += 1
            return super().__call__(*args, **kwargs)

        def invoke(self, *args, **kwargs):  # type: ignore[override]
            calls["n"] += 1
            return super().invoke(*args, **kwargs)

    class _CountingFakeLLM:
        def with_structured_output(self, _schema):
            return _CountingStructured(_stub_problem())

    monkeypatch.setattr(agent_problem, "get_llm", lambda *a, **k: _CountingFakeLLM())
    with pytest.raises(RuntimeError):
        generate_problem("爬楼梯", "medium", max_retries=1)
    # max_retries=1 → 应有 2 次 LLM 调用（首次 + 1 次重试）
    assert calls["n"] == 2


# ───────────────────────── verify_problem ─────────────────────────

def test_verify_problem_accepts_compilable_solution():
    d = {
        "title": "F",
        "optimal_solution": "class Solution:\n    def f(self, x: int) -> int:\n        return x\n",
        "brute_solution": "class Solution:\n    def f(self, x: int) -> int:\n        return x\n",
        "description": "Given an integer x, return it unchanged.",
        "examples": ["Input: x=5 -> 5"],
        "constraints": ["-10^4 <= x <= 10^4"],
        "starter_code": "class Solution:\n    def f(self, x: int) -> int:\n        pass\n",
    }
    assert verify_problem(d) is True


def test_verify_problem_rejects_chain_of_thought():
    d = {
        "title": "F",
        "optimal_solution": "class Solution:\n    def f(self, x: int) -> int:\n        return x\n",
        "description": "让我们先分析这道题，其实可以用双指针。",
        "examples": ["Input: x=5 -> 5"],
        "constraints": ["-10^4 <= x <= 10^4"],
        "starter_code": "class Solution:\n    def f(self, x: int) -> int:\n        pass\n",
    }
    assert verify_problem(d) is False


def test_verify_problem_rejects_syntax_error():
    d = {
        "title": "F",
        "optimal_solution": "class Solution:\n    def f(self, x: int) -> int\n        return x\n",  # 缺冒号
        "description": "Given an integer x, return it unchanged.",
        "examples": ["Input: x=5 -> 5"],
        "constraints": ["-10^4 <= x <= 10^4"],
        "starter_code": "class Solution:\n    def f(self, x: int) -> int:\n        pass\n",
    }
    assert verify_problem(d) is False


def test_verify_problem_derives_starter_code():
    optimal = (
        "class Solution:\n"
        "    def maxArea(self, height: List[int]) -> int:\n"
        "        return 0\n"
    )
    d = {
        "title": "Container With Most Water",
        "optimal_solution": optimal,
        "brute_solution": "class Solution:\n    def maxArea(self, height: List[int]) -> int:\n        return 0\n",
        "description": "Given n non-negative integers, find two lines that together with the x-axis forms a container containing the most water.",
        "examples": ["Input: height=[1,8,6,2,5,4,8,3,7] -> 49"],
        "constraints": ["n == height.length"],
        "starter_code": "",
    }
    assert verify_problem(d) is True
    assert "class Solution" in d["starter_code"]
    assert "def maxArea" in d["starter_code"]
    assert "height" in d["function_signature"]


def test_verify_problem_rejects_stub_optimal():
    """空题目（如「爬楼梯」桩解 class Solution: def solution(self): pass）必须被拒。"""
    d = {
        "title": "Climbing Stairs",
        "optimal_solution": "class Solution:\n    def climbStairs(self, n: int) -> int:\n        pass\n",
        "description": "You are climbing a staircase with n steps. Each time you can either climb 1 or 2 steps. In how many distinct ways can you climb to the top?",
        "examples": ["Input: n=2 -> 2"],
        "constraints": ["1 <= n <= 45"],
        "starter_code": "class Solution:\n    def climbStairs(self, n: int) -> int:\n        pass\n",
    }
    assert verify_problem(d) is False


def test_verify_problem_rejects_empty_content():
    """标题/描述/示例/约束任一为空都应被拒，触发重试而非返回空题。"""
    base = {
        "title": "Empty",
        "optimal_solution": "class Solution:\n    def f(self, x: int) -> int:\n        return x\n",
        "description": "Given an integer x, return it unchanged.",
        "examples": ["Input: x=5 -> 5"],
        "constraints": ["-10^4 <= x <= 10^4"],
        "starter_code": "class Solution:\n    def f(self, x: int) -> int:\n        pass\n",
    }
    for field in ("title", "description", "examples", "constraints"):
        bad = {**base, field: [] if field in ("examples", "constraints") else ""}
        assert verify_problem(bad) is False


# ───────────────────────── _extract_code ─────────────────────────

def test_extract_code_strips_fence():
    assert _extract_code("```python\nprint(1)\n```") == "print(1)"


def test_extract_code_plain():
    assert _extract_code("print(1)") == "print(1)"


# ───────────────────────── _flat_to_problem ─────────────────────────

def test_flat_to_problem_filters_to_model_fields():
    flat = {
        "title": "X",
        "topic": "数组",
        "difficulty": "easy",
        "description": "d",
        "function_signature": "f: int -> int",
        "starter_code": "sc",
        "optimal_solution": "os",
        "test_cases": [],
        "source": "adapter",  # 多余字段应被丢弃
    }
    p = _flat_to_problem(flat)
    assert isinstance(p, Problem)
    assert p.title == "X"
    assert not hasattr(p, "source")


# ───────────────────────── 出题不依赖外部工具 ─────────────────────────
# 出题仅走原生 LLM + 静态兜底，不再经由 skill-engine（adapter/cli）出题通道；
# 相关 skill-engine 出题测试已移除。题解功能（generate_detailed_solution）仍走
# skill-engine，属另一功能，见下方独立用例。


def test_generate_detailed_solution_success(monkeypatch):
    monkeypatch.setattr(
        agent_problem._adapter, "generate_detailed_solution",
        lambda *a, **k: "# 详细题解\n\n这是题解。",
    )
    assert "题解" in generate_detailed_solution("题目描述...")


def test_generate_detailed_solution_failure_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("skill down")

    monkeypatch.setattr(agent_problem._adapter, "generate_detailed_solution", _boom)
    assert generate_detailed_solution("题目描述...") is None


# ───────────────────────── ProblemAgent.generate 降级链 ─────────────────────────

def test_agent_generate_llm_channel(monkeypatch):
    monkeypatch.setattr(agent_problem, "get_llm", lambda *a, **k: _FakeLLM(_valid_problem()))
    out = ProblemAgent("数组", "easy").generate()
    assert out.ok
    assert out.channel == ProblemChannel.LLM
    assert isinstance(out.problem, Problem)


def test_agent_generate_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(agent_problem, "generate_problem", _boom)

    flat = {
        "title": "Static Problem",
        "topic": "数组",
        "difficulty": "easy",
        "description": "static",
        "function_signature": "f: int -> int",
        "starter_code": "class Solution:\n    def f(self, x: int) -> int:\n        pass\n",
        "optimal_solution": "class Solution:\n    def f(self, x: int) -> int:\n        return x\n",
        "test_cases": [],
    }
    monkeypatch.setattr("code_tutor_agent.store.static_pool.get_static_problem",
                        lambda *a, **k: flat)
    out = ProblemAgent("数组", "easy").generate()
    assert out.ok
    assert out.channel == ProblemChannel.STATIC
    assert out.problem.title == "Static Problem"


def test_agent_generate_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(agent_problem, "generate_problem", _boom)
    monkeypatch.setattr("code_tutor_agent.store.static_pool.get_static_problem",
                        lambda *a, **k: None)
    out = ProblemAgent("数组", "easy").generate()
    assert not out.ok
    assert out.problem is None
    assert out.channel == ProblemChannel.STATIC


def test_problem_channel_str_and_outcome():
    out = GenerationOutcome(None, ProblemChannel.STATIC, error="x")
    assert str(out.channel) == "static"
    assert out.ok is False
