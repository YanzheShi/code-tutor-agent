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
    """根据 starter_code 中引用的数据结构类型，注入对应的结构体定义。

    检测策略：
    - 扫描 starter_code 中出现的 ListNode、TreeNode、Node、GraphNode 类型引用
      （如类型注解 ``: ListNode``、``-> TreeNode`` 等）
    - 如果 starter_code 中已经定义了同名类，则不再重复注入
    - 不再使用 keyword 匹配（避免 ``bst`` 误匹配 ``substring`` 等问题）

    返回空字符串表示不需要额外定义。
    """
    if not starter_code:
        return ""

    # 结构体常量映射：类型名 → 定义文本
    _STRUCT_MAP = {
        "ListNode": STRUCT_LINKED_LIST,
        "TreeNode": STRUCT_BINARY_TREE,
        "Node": STRUCT_NARY_TREE,
        "GraphNode": STRUCT_GRAPH,
    }

    # 扫描 starter_code 中引用了哪些结构体类型（类型注解、参数名等中的引用）
    # 使用 \b 确保单词边界，避免 "bst" 误匹配 "substring" 这类问题
    type_refs = set(re.findall(r"\b(ListNode|TreeNode|Node|GraphNode)\b", starter_code))

    # 去重：starter_code 已自带同名类定义时，不再重复前置注入。
    # 注意：只检测非注释行中的 class 定义，跳过 # class ListNode 这类注释。
    defined = set(re.findall(r"^[ \t]*class\s+(\w+)", starter_code or "", re.MULTILINE))

    result = []
    for cls_name in ("ListNode", "TreeNode", "Node", "GraphNode"):
        if cls_name in type_refs and cls_name not in defined:
            result.append(_STRUCT_MAP[cls_name])

    return "".join(result)