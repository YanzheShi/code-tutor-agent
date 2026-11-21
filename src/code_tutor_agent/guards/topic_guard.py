"""知识点 ↔ 数据结构一致性守卫（2026-11-21）。

背景：LLM 会把 ``topic`` 当标签照抄，实际却换成另一种数据结构。
2026-11-13 线上实测：``topic=链表`` 连出 4 道数组/图题（入参
``List[List[str]]`` / ``List[int]``），示例自洽后照常落库，还污染了画像。

职责边界
--------
纯静态：不执行代码、不调 LLM、不碰 DB。只拦「数据结构唯一确定」的知识点：

- 链表 → 必须引用 ``ListNode``；
- 二叉树 / 树 → 必须引用 ``TreeNode``；
- 图不拦：邻接表 / 邻接矩阵是合法表示，强推 ``GraphNode`` 会误伤；
- 线段树 / 字典树 / 二叉堆 / 树状数组等数组型结构不会被误判成 TreeNode。
"""

from __future__ import annotations

import re

_LINKED_LIST_HINTS = ("链表", "linked")
_TREE_EXACT = frozenset({
    "树", "tree", "binary tree", "binary_tree", "binarytree", "bst",
})
# 数组型结构：归一化描述里可能提到「完全二叉树」（如「堆」），必须先排除。
_NON_TREE_STRUCT_HINTS = (
    "堆", "优先队列", "heap", "线段树", "字典树", "前缀树", "trie", "树状数组",
)


def required_struct_for_topic(topic: str) -> str | None:
    """返回该知识点强制的 LeetCode 节点类名；无需强制时返回 None。"""
    t = (topic or "").strip().lower()
    if not t:
        return None
    if any(hint in t for hint in _LINKED_LIST_HINTS):
        return "ListNode"
    if any(hint in t for hint in _NON_TREE_STRUCT_HINTS):
        return None
    if (
        t in _TREE_EXACT
        or "二叉树" in t
        or "二叉搜索树" in t
        or "二叉查找树" in t
        or t.startswith("树的")
    ):
        return "TreeNode"
    return None


def _code_only(text: str) -> str:
    """去掉整行/行尾注释，避免只靠 ``# ListNode`` 注释满足守卫。"""
    return re.sub(r"#[^\n]*", "", text or "")


def topic_structure_issues(
    topic: str,
    *,
    starter_code: str = "",
    function_signature: str = "",
    optimal_solution: str = "",
) -> list[str]:
    """检查题目是否真的使用该知识点要求的数据结构，返回问题列表。"""
    required = required_struct_for_topic(topic)
    if required is None:
        return []
    missing = [
        name
        for name, text in (
            ("starter_code", starter_code),
            ("function_signature", function_signature),
            ("optimal_solution", optimal_solution),
        )
        if not re.search(rf"\b{required}\b", _code_only(text))
    ]
    if not missing:
        return []
    return [
        f"题型跑偏：知识点为「{topic}」，但 {' / '.join(missing)} 未引用 {required}；"
        f"{topic}题必须以 {required} 作为输入/输出并操作其指针字段"
    ]
