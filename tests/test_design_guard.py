"""设计类题目围栏（design guard）单测。

覆盖三层入口：
1. 检测器本体（is_design_style_code / extract_main_classes / mentions_design_topic）
2. LeetCode 导入围栏（fetch_problem 对设计题 slug 抛 ValueError）
3. 出题硬校验（verify_problem / CodeVerifier 拒绝设计类生成结果）
4. 对话意图硬守护（analyze_user_intent 关键词兜底）
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from code_tutor_agent.guards.design_guard import (
    extract_main_classes,
    is_design_style_code,
    mentions_design_topic,
)


# ──────────────────────────────────────────────
# 1) 检测器本体
# ──────────────────────────────────────────────

SOLUTION_SNIPPET = """\
class Solution:
    def max_area(self, height: List[int]) -> int:
        pass
"""

TREE_SNIPPET = """\
# Definition for a binary tree node.
class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

class Solution:
    def invert_tree(self, root: Optional[TreeNode]) -> Optional[TreeNode]:
        pass
"""

LRU_SNIPPET = """\
class LRUCache:

    def __init__(self, capacity: int):
        pass

    def get(self, key: int) -> int:
        pass

    def put(self, key: int, value: int) -> None:
        pass
"""

MIN_STACK_SNIPPET = """\
class MinStack:

    def __init__(self):
        pass

    def push(self, val: int) -> None:
        pass
"""

COMMENT_TRICK_SNIPPET = """\
# class Solution:
#   看起来像功能题，其实是注释
class LRUCache:
    def __init__(self, capacity: int):
        pass
"""


def test_solution_snippet_is_not_design():
    assert is_design_style_code(SOLUTION_SNIPPET) is False


def test_tree_snippet_with_solution_is_not_design():
    assert is_design_style_code(TREE_SNIPPET) is False


def test_lru_snippet_is_design():
    assert is_design_style_code(LRU_SNIPPET) is True
    assert extract_main_classes(LRU_SNIPPET) == ["LRUCache"]


def test_min_stack_snippet_is_design():
    assert is_design_style_code(MIN_STACK_SNIPPET) is True


def test_comment_only_solution_does_not_fool_detector():
    assert is_design_style_code(COMMENT_TRICK_SNIPPET) is True


def test_empty_and_fence_snippets_are_not_design():
    assert is_design_style_code("") is False
    assert is_design_style_code("def foo(): pass") is False
    fenced = "```python\n" + LRU_SNIPPET + "```"
    assert is_design_style_code(fenced) is True


def test_mentions_design_topic():
    assert mentions_design_topic("出一道LRU缓存的题") is True
    assert mentions_design_topic("我想练 Trie 前缀树") is True
    assert mentions_design_topic("用栈实现队列") is True
    assert mentions_design_topic("出一道数组双指针的题") is False
    assert mentions_design_topic("") is False


def test_mentions_design_topic_ignores_generic_ds_topics():
    """栈/队列/堆/哈希表等数据结构主题本身允许，绝不能误判为设计类。

    设计类围栏的判定依据应是『生成的代码形态』（主类非 Solution），
    而非主题名；这些主题既有设计题也有应用题，关键词兜底必须放行它们，
    否则普通『队列中等难度』请求会被对话层软拒（2026-09-06 实踩的坑）。
    """
    allowed_topics = [
        "请出一道中等难度、主题关于「队列」的算法题。",
        "队列 中等难度",
        "栈 中等",
        "堆 中等难度",
        "哈希表 中等",
        "链表 简单",
        "二叉树 困难",
    ]
    for text in allowed_topics:
        assert mentions_design_topic(text) is False, f"普通数据结构主题被误判为设计类：{text!r}"


# ──────────────────────────────────────────────
# 2) LeetCode 导入围栏
# ──────────────────────────────────────────────

def _fake_graphql(snippet_code: str) -> dict:
    """构造 fetch_problem 的 GraphQL 响应（codeSnippets 为 dict 列表，与真实结构一致）。"""
    return {
        "data": {
            "question": {
                "title": "LRU Cache",
                "titleSlug": "lru-cache",
                "translatedTitle": "LRU 缓存",
                "difficulty": "MEDIUM",
                "content": "<p>设计并实现一个 LRU 缓存。</p>",
                "translatedContent": None,
                "exampleTestcases": "",
                "topicTags": [{"name": "design", "translatedName": "设计"}],
                "hints": [],
                "codeSnippets": [{"langSlug": "python3", "code": snippet_code}],
            }
        }
    }


def test_fetch_problem_rejects_design_slug(monkeypatch):
    """fetch_problem 拿到设计题模板（主类非 Solution）→ 抛 ValueError。"""
    from code_tutor_agent.leetcode import leetcode_fetcher

    monkeypatch.setattr(
        leetcode_fetcher, "_post_with_retry", lambda req, max_retries=3, timeout=15: _fake_graphql(LRU_SNIPPET)
    )
    with pytest.raises(ValueError, match="设计类题目"):
        leetcode_fetcher.fetch_problem("lru-cache")


def test_fetch_problem_allows_solution_slug(monkeypatch):
    """功能题（class Solution 模板）→ 正常返回，不被围栏拦截。"""
    from code_tutor_agent.leetcode import leetcode_fetcher

    def _fake_post(req, max_retries=3, timeout=15):
        payload = _fake_graphql(SOLUTION_SNIPPET)
        payload["data"]["question"].update({
            "title": "Container With Most Water",
            "translatedTitle": "盛最多水的容器",
            "content": "<p>给定一个数组……</p>",
            "topicTags": [{"name": "two-pointers", "translatedName": "双指针"}],
        })
        return payload

    monkeypatch.setattr(leetcode_fetcher, "_post_with_retry", _fake_post)
    problem = leetcode_fetcher.fetch_problem("container-with-most-water")
    assert problem.title == "盛最多水的容器"


# ──────────────────────────────────────────────
# 3) 出题硬校验
# ──────────────────────────────────────────────

def _base_problem_dict(**overrides) -> dict:
    d = {
        "title": "设计一个最小栈",
        "description": "设计一个支持 push、pop、top 操作并能在常数时间内检索到最小元素的栈。",
        "difficulty": "medium",
        "topic": "栈",
        "examples": ["输入：[\"MinStack\",\"push\",...] 输出：[null,null,...]"],
        "constraints": ["-2^31 <= val <= 2^31 - 1"],
        "starter_code": MIN_STACK_SNIPPET,
        "optimal_solution": (
            "class MinStack:\n"
            "    def __init__(self):\n"
            "        self.st = []\n"
            "        self.min_st = []\n"
        ),
        "brute_solution": "",
        "function_signature": "",
    }
    d.update(overrides)
    return d


def test_verify_problem_rejects_design_optimal():
    from code_tutor_agent.agents.agent_problem import verify_problem

    assert verify_problem(_base_problem_dict()) is False


def test_verify_problem_rejects_design_starter():
    from code_tutor_agent.agents.agent_problem import verify_problem

    d = _base_problem_dict(
        optimal_solution=(
            "class Solution:\n"
            "    def solve(self) -> int:\n"
            "        return 0\n"
        ),
    )
    assert verify_problem(d) is False


def test_verify_problem_accepts_solution_problem():
    from code_tutor_agent.agents.agent_problem import verify_problem

    d = _base_problem_dict(
        title="两数之和",
        description="给定一个整数数组 nums 和一个目标值 target，找出和为目标值的两个整数下标。",
        topic="数组",
        examples=["输入：nums = [2,7,11,15], target = 9 输出：[0,1]"],
        starter_code=SOLUTION_SNIPPET,
        optimal_solution=(
            "class Solution:\n"
            "    def max_area(self, height):\n"
            "        seen = {}\n"
            "        for i, n in enumerate(height):\n"
            "            if n in seen:\n"
            "                return [seen[n], i]\n"
            "            seen[n] = i\n"
            "        return []\n"
        ),
    )
    assert verify_problem(d) is True


def test_code_verifier_flags_design_starter():
    from code_tutor_agent.generation.verifier import CodeVerifier

    class Draft:
        title = "LRU"
        description = "设计一个 LRU 缓存结构。" * 2
        starter_code = LRU_SNIPPET
        function_signature = ""
        from_leetcode = False
        optimal_solution = "class LRUCache:\n    pass\n"
        brute_solution = ""

    ok, issues = CodeVerifier().verify(Draft())
    assert ok is False
    assert any("设计类" in i for i in issues)


# ──────────────────────────────────────────────
# 4) 对话意图硬守护
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_intent_blocks_design_topic():
    """用户点名 LRU → 即使 LLM 意图判定 is_ready=True，也被硬守护拦下。"""
    from unittest.mock import patch

    from code_tutor_agent.agents import agent_dialog
    from code_tutor_agent.schemas.state import Message

    history = [
        Message(role="tutor", content="想练什么类型的题？"),
        Message(role="user", content="给我出一道 LRU 缓存的题"),
    ]

    with patch.object(agent_dialog, "_build_profile_summary", return_value=""), \
         patch.object(agent_dialog, "_build_memory_summary", return_value=""), \
         patch.object(agent_dialog, "get_llm", side_effect=Exception("LLM unavailable")):
        # get_llm 抛错 → 走 _fallback_parse_intent；关键词兜底仍应拦截
        intent = await agent_dialog.analyze_user_intent(history)

    assert intent.is_ready is False
    assert "设计类" in (intent.next_message or "")


@pytest.mark.asyncio
async def test_analyze_intent_blocks_design_topic_from_llm():
    """LLM 意图无视 prompt 约束把 LRU 设为 is_ready=True → 硬守护强制改写。"""
    from unittest.mock import MagicMock, patch

    from code_tutor_agent.agents import agent_dialog
    from code_tutor_agent.agents.agent_dialog import DialogIntent
    from code_tutor_agent.schemas.state import Message

    history = [
        Message(role="tutor", content="想练什么类型的题？"),
        Message(role="user", content="出一道最小栈的题"),
    ]

    mock_structured = MagicMock()
    mock_structured.invoke.return_value = DialogIntent(
        topic="最小栈",
        difficulty="medium",
        is_ready=True,
        next_message="好的，马上为你生成最小栈题目 🚀",
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.invoke.return_value = MagicMock(tool_calls=None)
    mock_llm.with_structured_output.return_value = mock_structured

    with patch.object(agent_dialog, "_build_profile_summary", return_value=""), \
         patch.object(agent_dialog, "_build_memory_summary", return_value=""), \
         patch.object(agent_dialog, "get_llm", return_value=mock_llm):
        intent = await agent_dialog.analyze_user_intent(history)

    assert intent.is_ready is False
    assert "设计类" in (intent.next_message or "")


@pytest.mark.asyncio
async def test_analyze_intent_allows_generic_ds_topic():
    """普通数据结构主题（队列中等难度）绝不能被判为设计类而软拒。

    这里走 LLM 不可用的兜底分支（正则抽 topic=队列 + 难度=medium → is_ready=True），
    验证对话意图硬守护不会误伤普通栈/队列/堆/哈希表主题——它们应放行到出题层，
    由代码的 class Solution 校验兜底（2026-09-06 修复的设计类围栏误伤）。
    """
    from unittest.mock import patch

    from code_tutor_agent.agents import agent_dialog
    from code_tutor_agent.schemas.state import Message

    history = [
        Message(role="tutor", content="想练什么类型的题？"),
        Message(role="user", content="请出一道中等难度、主题关于「队列」的算法题。"),
    ]

    with patch.object(agent_dialog, "_build_profile_summary", return_value=""), \
         patch.object(agent_dialog, "_build_memory_summary", return_value=""), \
         patch.object(agent_dialog, "get_llm", side_effect=Exception("LLM unavailable")):
        intent = await agent_dialog.analyze_user_intent(history)

    assert intent.topic == "队列"
    assert intent.difficulty == "medium"
    assert intent.is_ready is True
    assert "设计类" not in (intent.next_message or "")
