"""回归 2026-11-13：topic=链表 时首题数组题必须被打回并重采样。

当天链路：LLM 第 1 题把「地铁换乘」写成 List[List[str]] 数组题，示例自洽后
直接落库；第 3 次重采样才出到真正的 ListNode 题。本用例锁住：

1. 数组题在静态阶段被判 TOPIC_MISMATCH（不跑沙箱、不落库）；
2. 第二轮重采样带上的 retry hint 明确要求 ListNode、禁止数组替代；
3. 第二题（真链表）正常落库。
"""

from __future__ import annotations

from types import SimpleNamespace

from code_tutor_agent.generation import ProblemGenerationAgent
from code_tutor_agent.generation.state import GenerationContext, ProblemDraft
from code_tutor_agent.generation.verifier import CodeVerifier

_ARRAY_OPTIMAL = (
    "class Solution:\n"
    "    def process(self, stations: List[List[str]]) -> List[str]:\n"
    "        return []\n"
)
_LINKED_OPTIMAL = (
    "class Solution:\n"
    "    def merge(self, l1: 'ListNode', l2: 'ListNode') -> 'ListNode':\n"
    "        return l1\n"
)


def _array_draft() -> ProblemDraft:
    return ProblemDraft(
        topic="链表",
        difficulty="medium",
        title="地铁换乘顺序去重生成",
        description="给定二维字符串列表，按顺序展开并去重，返回站点序列。",
        starter_code=(
            "class Solution:\n"
            "    def process(self, stations: List[List[str]]) -> List[str]:\n"
            "        pass\n"
        ),
        optimal_solution=_ARRAY_OPTIMAL,
        brute_solution=_ARRAY_OPTIMAL,
        examples=['Input: stations = [["A"]]\nOutput: ["A"]\n解释：只有一个站点。'],
        constraints=["1 <= stations.length <= 10^5"],
        function_signature="stations: List[List[str]] -> List[str]",
    )


def _linked_draft() -> ProblemDraft:
    return ProblemDraft(
        topic="链表",
        difficulty="medium",
        title="好友推荐流合并",
        description="给定两个升序单链表，合并为一个升序单链表并返回头节点。",
        starter_code=(
            "class ListNode:\n"
            "    def __init__(self, val=0, next=None):\n"
            "        self.val = val\n"
            "        self.next = next\n\n"
            "class Solution:\n"
            "    def merge(self, l1: 'ListNode', l2: 'ListNode') -> 'ListNode':\n"
            "        pass\n"
        ),
        optimal_solution=_LINKED_OPTIMAL,
        brute_solution=_LINKED_OPTIMAL,
        examples=["Input: l1 = [1], l2 = [2]\nOutput: [1,2]\n解释：合并后保持升序。"],
        constraints=["0 <= 链表长度 <= 1000"],
        function_signature="l1: 'ListNode', l2: 'ListNode' -> 'ListNode'",
    )


class _Store:
    def __init__(self):
        self.saved: list[ProblemDraft] = []

    def save(self, draft: ProblemDraft) -> tuple[int, bool]:
        self.saved.append(draft)
        return 100 + len(self.saved), False

    def unac_problem(self, *a, **k):
        return None

    def static_problem(self, *a, **k):
        return None


class _Sandbox:
    """示例沙箱恒 Passed；不提供 random_inputs，质量闸自动跳过。"""

    def __init__(self):
        self.calls = 0

    def struct_prologue(self, *a, **k):
        return ""

    def run_batch(self, code, cases, function_signature="", timeout=5.0):
        self.calls += 1
        return [
            SimpleNamespace(status="Passed", detail="[1]", input_args=tc.get("input_args"))
            for tc in cases
        ]


def test_array_problem_resampled_with_topic_hint():
    prompts: list[str] = []
    drafts = [_array_draft(), _linked_draft()]

    def fake_generate(topic, difficulty, user_suffix=None):
        prompts.append(user_suffix or "")
        return drafts[len(prompts) - 1]

    store = _Store()
    sandbox = _Sandbox()
    agent = ProblemGenerationAgent(
        leetcode=SimpleNamespace(
            parse_examples=lambda examples, starter: [
                {"input_args": ["[1]"], "expected_output": "", "explanation": ""},
            ],
        ),
        llm=SimpleNamespace(generate_problem=fake_generate),
        store=store,
        sandbox=sandbox,
        verifier=CodeVerifier(),
    )

    result = agent.run(GenerationContext(topic="链表", difficulty="medium"))

    assert result.ok, result.error
    assert len(prompts) == 2
    assert "[重试]" not in prompts[0]             # 首轮只有场景注入，没有失败信号
    assert "ListNode" not in prompts[0]
    assert "[重试]" in prompts[1]
    assert "ListNode" in prompts[1]
    assert "禁止改用数组" in prompts[1]
    assert sandbox.calls == 1                     # 数组题静态失败，不跑沙箱
    assert [d.title for d in store.saved] == ["好友推荐流合并"]
