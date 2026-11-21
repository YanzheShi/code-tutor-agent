"""知识点 ↔ 数据结构一致性守卫（guards.topic_guard）单元测试。

回归 2026-11-13：用户要链表题，LLM 连出数组/图题，示例自洽后照常落库。
"""

from __future__ import annotations

import pytest

from code_tutor_agent.generation.problem_generation_agent import _selfcheck_retry_hint
from code_tutor_agent.guards.topic_guard import (
    required_struct_for_topic,
    topic_structure_issues,
)

_LINKED_ARRAY_PROBLEM = dict(
    starter_code=(
        "class Solution:\n"
        "    def process_transfers(self, stations: List[List[str]]) -> List[str]:\n"
        "        pass\n"
    ),
    function_signature="stations: List[List[str]] -> List[str]",
    optimal_solution=(
        "class Solution:\n"
        "    def process_transfers(self, stations: List[List[str]]) -> List[str]:\n"
        "        return []\n"
    ),
)

_LINKED_LIST_PROBLEM = dict(
    starter_code=(
        "class ListNode:\n"
        "    def __init__(self, val=0, next=None):\n"
        "        self.val = val\n"
        "        self.next = next\n\n"
        "class Solution:\n"
        "    def merge(self, l1: 'ListNode', l2: 'ListNode') -> 'ListNode':\n"
        "        pass\n"
    ),
    function_signature="l1: 'ListNode', l2: 'ListNode' -> 'ListNode'",
    optimal_solution=(
        "class Solution:\n"
        "    def merge(self, l1: 'ListNode', l2: 'ListNode') -> 'ListNode':\n"
        "        return l1\n"
    ),
)


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("链表", "ListNode"),
        ("链表基础", "ListNode"),
        ("linkedlist_basics", "ListNode"),
        ("linked-list", "ListNode"),
        ("二叉树", "TreeNode"),
        ("二叉搜索树", "TreeNode"),
        ("平衡二叉树", "TreeNode"),
        ("树", "TreeNode"),
        ("树的遍历", "TreeNode"),
        ("tree", "TreeNode"),
        ("binary tree", "TreeNode"),
        ("bst", "TreeNode"),
        # 数组型结构不能被误判成 TreeNode
        ("线段树", None),
        ("字典树", None),
        ("前缀树", None),
        ("二叉堆", None),
        ("堆", None),
        ("优先队列", None),
        ("树状数组", None),
        # 图不强制 GraphNode（邻接表/邻接矩阵合法）
        ("图", None),
        ("拓扑排序", None),
        ("数组", None),
        ("", None),
    ],
)
def test_required_struct_for_topic(topic, expected):
    assert required_struct_for_topic(topic) == expected


def test_array_problem_labeled_linked_list_is_rejected():
    issues = topic_structure_issues("链表", **_LINKED_ARRAY_PROBLEM)
    assert issues
    assert "题型跑偏" in issues[0]
    assert "ListNode" in issues[0]


def test_real_linked_list_problem_passes():
    assert topic_structure_issues("链表", **_LINKED_LIST_PROBLEM) == []


def test_normalized_heap_topic_not_mistaken_for_tree():
    """「堆」的归一化描述含「完全二叉树」，但不能被当成 TreeNode 题。"""
    from code_tutor_agent.generation.gateways.llm import normalize_topic_for_generation

    normalized = normalize_topic_for_generation("堆")
    assert "二叉树" in normalized
    assert required_struct_for_topic(normalized) is None
    assert required_struct_for_topic("堆") is None


def test_comment_only_mention_does_not_satisfy_guard():
    """只有 # ListNode 注释、代码本身仍是数组 → 不算链表题。"""
    issues = topic_structure_issues(
        "链表",
        starter_code=(
            "# Definition for singly-linked list: ListNode\n"
            "class Solution:\n"
            "    def f(self, nums: List[int]) -> int:\n"
            "        pass\n"
        ),
        function_signature="nums: List[int] -> int",
        optimal_solution=(
            "# 这里本可用 ListNode，但实际按数组处理\n"
            "class Solution:\n"
            "    def f(self, nums: List[int]) -> int:\n"
            "        return 0\n"
        ),
    )
    assert issues


def test_tree_topic_rejects_array_problem():
    issues = topic_structure_issues("二叉树", **_LINKED_ARRAY_PROBLEM)
    assert issues and "TreeNode" in issues[0]


def test_non_struct_topic_never_flagged():
    assert topic_structure_issues("数组", **_LINKED_ARRAY_PROBLEM) == []
    assert topic_structure_issues("图", **_LINKED_ARRAY_PROBLEM) == []


def test_retry_hint_adds_struct_constraint_for_linked_list():
    hint = _selfcheck_retry_hint("链表")
    assert "ListNode" in hint
    assert "禁止改用数组" in hint


def test_retry_hint_stays_generic_for_array_topic():
    assert _selfcheck_retry_hint("数组") == _selfcheck_retry_hint("")
    assert "ListNode" not in _selfcheck_retry_hint("数组")
