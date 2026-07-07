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