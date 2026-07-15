"""LeetCode 常用数据结构（TreeNode / ListNode / Node）。

注入到判题 runner 的全局命名空间中，让用户代码可以直接使用
``TreeNode``、``ListNode``、``Node`` 而无需手动定义或 import。

用法：
    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE
    code = INJECT_PROLOGUE + user_code
"""

from __future__ import annotations

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
    返回空字符串表示不需要额外定义。
    """
    combined = f"{topic} {description} {starter_code}".lower()

    parts: list[str] = []

    # 链表检测
    _list_keywords = ["链表", "linkedlist", "listnode", "singly-linked", "单链表", "双链表"]
    if any(k in combined for k in _list_keywords):
        parts.append(STRUCT_LINKED_LIST)

    # N 叉树检测（在二叉树之前，避免"二叉树"误匹配）
    _nary_keywords = ["n叉树", "n 叉树", "node", "trie", "前缀树"]
    if any(k in combined for k in _nary_keywords):
        parts.append(STRUCT_NARY_TREE)

    # 二叉树检测
    _tree_keywords = ["二叉树", "二叉搜索树", "bst", "treenode", "binary tree", "binary search tree",
                      "平衡二叉树", "完全二叉树", "线段树", "二叉树节点", "树的", "树节点",
                      "前序遍历", "中序遍历", "后序遍历", "层序遍历", "树的遍历"]
    if any(k in combined for k in _tree_keywords):
        if STRUCT_BINARY_TREE not in parts:  # 避免重复（如 N 叉树已添加 Node）
            parts.append(STRUCT_BINARY_TREE)

    # 图检测
    _graph_keywords = ["图", "graph", "graphnode", "邻接表", "邻接矩阵", "拓扑排序",
                       "bfs", "dfs", "dijkstra", "floyd", "最短路径"]
    if any(k in combined for k in _graph_keywords):
        parts.append(STRUCT_GRAPH)

    return "".join(parts)