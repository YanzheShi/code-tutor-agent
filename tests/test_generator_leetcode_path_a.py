"""针对 generation 包路径 A（LeetCode 导入）与降级链的单元测试。

覆盖新子 Agent 架构（docs/generation-subagent-design.md §4）的关键决策：
* URL 导入 → LeetCodeGateway.fetch + slug 回填 → channel=leetcode_import；
* 导入失败 → 直接报错提示用户（ok=False + 错误原因），绝不静默换题/生成原创题；
* 全链路失败 → GenerationResult(ok=False) + fallback_chain。
（LeetCode 解析已收口到 generation 包，不再接受预解析 dict。）
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.generation import ProblemGenerationAgent  # noqa: E402
from code_tutor_agent.generation.state import (  # noqa: E402
    GenerationContext,
    ProblemDraft,
)


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


def _static_draft() -> ProblemDraft:
    return ProblemDraft(
        topic="数组", difficulty="easy", title="Static Fallback",
        description="静态兜底题", starter_code="class Solution:\n    def f(self, a, b): pass\n",
        optimal_solution="class Solution:\n    def f(self, a, b): return a\n",
        test_cases=[{"input_args": ["1", "2"], "expected_output": "1", "explanation": "s"}],
    )


class _RecordingStore:
    """save 记录 draft；unac/static 可配置。"""

    def __init__(self):
        self.saved: list[ProblemDraft] = []
        self.unac: ProblemDraft | None = None
        self.static: ProblemDraft | None = _static_draft()

    def save(self, draft: ProblemDraft) -> int:
        self.saved.append(draft)
        return len(self.saved)

    def unac_problem(self, *a, **k):
        return self.unac

    def static_problem(self, *a, **k):
        return self.static


def _build_agent(store, *, fetch=None, listing=None, gen_optimal=None):
    leetcode = SimpleNamespace(
        fetch=fetch or (lambda slug: _make_lc_data()),
        list=lambda *a, **k: listing or [],
        to_lc_dict=lambda p: _make_lc_data(),
        parse_examples=lambda examples, starter: [],
        extract_signature=lambda s: "nums: list[int], target: int -> list[int]",
    )
    llm = SimpleNamespace(
        generate_problem=lambda *a, **k: None,
        generate_optimal=gen_optimal or (
            lambda *a, **k: (
                "class Solution:\n"
                "    def twoSum(self, nums, target):\n"
                "        return [0, 1]\n"
            )
        ),
        generate_dual=lambda *a, **k: None,
    )
    sandbox = SimpleNamespace(
        struct_prologue=lambda *a, **k: "",
        compile=lambda code: True,
        run_solution=lambda *a, **k: [],
        random_inputs=lambda *a, **k: [],
        sanitize=lambda *a, **k: None,
        needs_sorted_inputs=lambda *a, **k: False,
    )
    return ProblemGenerationAgent(leetcode=leetcode, llm=llm, store=store, sandbox=sandbox)


def test_import_channel_and_constraints():
    """URL 导入：channel=leetcode_import，constraints 与可见用例随 draft 落库。"""
    store = _RecordingStore()
    agent = _build_agent(store)
    ctx = GenerationContext(
        topic="数组", difficulty="easy",
        lc_url="https://leetcode.cn/problems/two-sum/",
    )

    result = agent.run(ctx)

    assert result.ok
    assert result.channel == "leetcode_import"
    assert result.draft is not None
    assert store.saved
    saved = store.saved[0]
    assert saved.constraints == _make_lc_data()["constraints"]
    assert saved.test_cases[0]["expected_output"] == "[0, 1]"
    assert saved.optimal_solution  # 缺最优解时由 generate_optimal 补上
    # URL 导入回填 source_slug，from_leetcode 据此判定来源
    assert saved.from_leetcode is True


def test_import_persists_leetcode_source():
    """URL 导入落库须标 source=leetcode / novelty 9.0。"""
    from code_tutor_agent.generation.gateways.store import draft_to_problem_dict

    store = _RecordingStore()
    agent = _build_agent(store)
    ctx = GenerationContext(
        topic="数组", difficulty="easy",
        lc_url="https://leetcode.cn/problems/two-sum/",
    )

    result = agent.run(ctx)

    assert result.ok and result.draft is not None
    flat = draft_to_problem_dict(result.draft)
    assert flat["source"] == "leetcode"
    assert flat["novelty_score"] == 9.0


def test_import_via_url_fetch_backfills_slug():
    """URL 导入：走 fetch，draft 回填 source_slug，通道仍为 leetcode_import。"""
    store = _RecordingStore()
    agent = _build_agent(store)
    ctx = GenerationContext(topic="数组", difficulty="easy",
                            lc_url="https://leetcode.cn/problems/reverse-integer/")

    result = agent.run(ctx)

    assert result.ok
    assert result.channel == "leetcode_import"
    assert result.draft.source_slug == "reverse-integer"


def test_import_failure_reports_error_no_fallback():
    """导入失败 → 直接报错（ok=False + channel=leetcode_import），绝不静默换题/原创。"""
    store = _RecordingStore()
    llm_calls = {"problem": 0}

    def counting_gen(*a, **k):
        llm_calls["problem"] += 1
        return None

    leetcode = SimpleNamespace(
        fetch=lambda slug: (_ for _ in ()).throw(RuntimeError("network")),
        list=lambda *a, **k: [],
        to_lc_dict=lambda p: _make_lc_data(),
        parse_examples=lambda examples, starter: [],
        extract_signature=lambda s: "nums: list[int], target: int -> list[int]",
    )
    llm = SimpleNamespace(
        generate_problem=counting_gen,
        generate_optimal=lambda *a, **k: "class Solution: pass",
        generate_dual=lambda *a, **k: None,
    )
    sandbox = SimpleNamespace(
        struct_prologue=lambda *a, **k: "",
        compile=lambda code: True,
        run_solution=lambda *a, **k: [],
        random_inputs=lambda *a, **k: [],
        sanitize=lambda *a, **k: None,
        needs_sorted_inputs=lambda *a, **k: False,
    )
    agent = ProblemGenerationAgent(leetcode=leetcode, llm=llm, store=store, sandbox=sandbox)
    ctx = GenerationContext(topic="数组", difficulty="easy",
                            lc_url="https://leetcode.cn/problems/reverse-integer/")

    result = agent.run(ctx)

    # 导入失败：直接报错，不进兜底链、不落库、不插 LLM 原创
    assert not result.ok
    assert result.channel == "leetcode_import"
    assert result.fallback_chain == []
    assert result.error  # 失败原因已透传
    assert llm_calls["problem"] == 0           # 仍不得插入 LLM 原创尝试
    assert store.saved == []                   # 绝不静默落静态题


def test_import_optimal_generation_failure_reports_error():
    """最优解生成失败（generate_optimal 返回 None）→ 导入直接报错，绝不静默落库。

    与 test_import_failure_reports_error_no_fallback 的区别：fetch 成功（题目已解析），
    但参考解生成失败——这属于「坏题」，同样不能静默写库，必须报错让用户重试/换链接。
    """
    store = _RecordingStore()
    leetcode = SimpleNamespace(
        fetch=lambda slug: _make_lc_data(),
        list=lambda *a, **k: [],
        to_lc_dict=lambda p: _make_lc_data(),
        parse_examples=lambda examples, starter: [],
        extract_signature=lambda s: "nums: list[int], target: int -> list[int]",
    )
    llm = SimpleNamespace(
        generate_problem=lambda *a, **k: None,
        generate_optimal=lambda *a, **k: None,  # 模拟最优解生成失败
        generate_dual=lambda *a, **k: None,
    )
    sandbox = SimpleNamespace(
        struct_prologue=lambda *a, **k: "",
        compile=lambda code: True,
        run_solution=lambda *a, **k: [],
        random_inputs=lambda *a, **k: [],
        sanitize=lambda *a, **k: None,
        needs_sorted_inputs=lambda *a, **k: False,
    )
    agent = ProblemGenerationAgent(leetcode=leetcode, llm=llm, store=store, sandbox=sandbox)
    ctx = GenerationContext(topic="数组", difficulty="easy",
                            lc_url="https://leetcode.cn/problems/two-sum/")

    result = agent.run(ctx)

    # 致命：直接报错，不进兜底链、不落库（避免一道无参考解的题目进入判题）
    assert not result.ok
    assert result.channel == "leetcode_import"
    assert result.error  # 明确错误信息已透传
    assert result.fallback_chain == []
    assert store.saved == []  # 绝不静默落库


def test_everything_fails_reports_chain():
    """LLM 失败 + 三条兜底全空（无 lc_url）→ ok=False 且带 fallback_chain。

    lc_url 导入失败已改为「直接报错、不走 chain」，故本用例用非 lc_url 路径
    覆盖「全链路失败带 chain」这条语义。
    """
    store = _RecordingStore()
    store.static = None
    agent = _build_agent(store)
    ctx = GenerationContext(topic="数组", difficulty="easy")  # 无 lc_url

    result = agent.run(ctx)

    assert not result.ok
    assert result.channel is None  # 未命中任何通道，无需透出
    assert result.fallback_chain == ["leetcode_pull", "db_unac", "static"]
    assert "所有通道均不可用" in result.error


def test_pull_channel_reported_when_hit():
    """降级链 PULL 命中 → channel=leetcode_pull。"""
    store = _RecordingStore()
    agent = _build_agent(store, listing=["two-sum"])
    ctx = GenerationContext(topic="数组", difficulty="easy")

    result = agent.run(ctx)

    assert result.ok
    assert result.channel == "leetcode_pull"
    assert result.draft.source_slug == "two-sum"
