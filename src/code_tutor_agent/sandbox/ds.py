"""LeetCode 常用数据结构（TreeNode / ListNode / Node）。

注入到判题 runner 的全局命名空间中，让用户代码可以直接使用
``TreeNode``、``ListNode``、``Node`` 而无需手动定义或 import。

用法：
    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE
    code = INJECT_PROLOGUE + user_code
"""

from __future__ import annotations

import re

# ── 注入的 prologue 文本，放在用户代码之前 ──
INJECT_PROLOGUE = """# ===== 平台预置类型注入 =====
from typing import *

# --- 链表节点 ---
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

# --- 二叉树节点 ---
class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

# --- N 叉树节点 ---
class Node:
    def __init__(self, val=None, children=None):
        self.val = val
        self.children = children if children is not None else []

"""

# ── 按 topic 分组的结构体定义（用于注入到 starter_code，不包含 import）──
STRUCT_LINKED_LIST = """# Definition for singly-linked list.
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

"""

STRUCT_BINARY_TREE = """# Definition for a binary tree node.
class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

"""

STRUCT_NARY_TREE = """# Definition for a Node.
class Node:
    def __init__(self, val=None, children=None):
        self.val = val
        self.children = children if children is not None else []

"""

STRUCT_GRAPH = """# Definition for a graph node.
class GraphNode:
    def __init__(self, val=0, neighbors=None):
        self.val = val
        self.neighbors = neighbors if neighbors is not None else []

"""


def get_struct_prologue(topic: str, description: str = "", starter_code: str = "") -> str:
    """根据题目 topic / 描述 / starter_code 返回应前置的结构体定义。

    检测策略：
    - 链表类关键词 → ListNode
    - 二叉树类关键词 → TreeNode
    - N 叉树 / 前缀树类关键词 → Node
    - 图类关键词 → GraphNode

    去重原则：**以出题 starter_code 自带的定义为主**。
    若 starter_code 中已经定义了同名类（如 LLM 已写出 ``class ListNode``），
    则不再重复前置注入，仅补充缺失的类型，避免出现多个无用/重复的类定义。

    返回空字符串表示不需要额外定义。
    """
    combined = f"{topic} {description} {starter_code}".lower()

    # 每个结构体常量 → 其定义的类名（用于去重）
    _STRUCT_CLASS = {
        "list": ("ListNode", STRUCT_LINKED_LIST),
        "nary": ("Node", STRUCT_NARY_TREE),
        "tree": ("TreeNode", STRUCT_BINARY_TREE),
        "graph": ("GraphNode", STRUCT_GRAPH),
    }

    candidates: list[tuple[str, str]] = []

    # 链表检测
    _list_keywords = ["链表", "linkedlist", "listnode", "singly-linked", "单链表", "双链表"]
    if any(k in combined for k in _list_keywords):
        candidates.append(_STRUCT_CLASS["list"])

    # N 叉树检测：只用强信号词。
    # 注意：不能放过于宽泛的 "node" —— 链表/二叉树的 starter 注释里常出现
    # "# Definition for a Node."，会误触发并注入一个根本用不上的 Node 类。
    _nary_keywords = ["n叉树", "n 叉树", "n-ary", "nary", "多叉树", "trie", "前缀树"]
    if any(k in combined for k in _nary_keywords):
        candidates.append(_STRUCT_CLASS["nary"])

    # 二叉树检测
    _tree_keywords = ["二叉树", "二叉搜索树", "bst", "treenode", "binary tree", "binary search tree",
                      "平衡二叉树", "完全二叉树", "线段树", "二叉树节点", "树的", "树节点",
                      "前序遍历", "中序遍历", "后序遍历", "层序遍历", "树的遍历"]
    if any(k in combined for k in _tree_keywords):
        candidates.append(_STRUCT_CLASS["tree"])

    # 图检测
    _graph_keywords = ["图", "graph", "graphnode", "邻接表", "邻接矩阵", "拓扑排序",
                       "bfs", "dfs", "dijkstra", "floyd", "最短路径"]
    if any(k in combined for k in _graph_keywords):
        candidates.append(_STRUCT_CLASS["graph"])

    # 去重：starter_code 已自带同名类定义时，不再重复前置注入
    defined = set(re.findall(r"class\s+(\w+)", starter_code or ""))
    result = []
    for cls_name, text in candidates:
        if cls_name in defined:
            continue
        result.append(text)
    return "".join(result)