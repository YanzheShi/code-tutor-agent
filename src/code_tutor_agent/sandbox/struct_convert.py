"""链表 / 树 结构的输入构建与输出序列化。

LeetCode 约定：链表/树类型的参数在测试用例中以「数组」形式给出
（如 ``[1,2,3,4,5]``、``[1,null,2,3]``），平台负责将其还原成 ``ListNode`` /
``TreeNode`` 对象再传入函数；返回值同理序列化回数组用于比对。

本模块提供：
- Python 侧转换函数（供需要时直接调用）
- ``HARNESS_STRUCT_SRC``：注入到 runner / judge0 harness 的辅助函数源码，
  依赖 ``INJECT_PROLOGUE`` 预置的 ``ListNode`` / ``TreeNode`` / ``Node`` 类。
"""

from __future__ import annotations

import ast
import json


# ── Python 侧转换函数 ──

def list_to_linkedlist(vals):
    """把列表还原成 ListNode 链表。"""
    if vals is None:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    dummy = ListNode(0)
    cur = dummy
    for v in vals:
        cur.next = ListNode(v)
        cur = cur.next
    return dummy.next


def linkedlist_to_list(node):
    """把 ListNode 链表序列化回列表。"""
    out = []
    while node:
        out.append(node.val)
        node = node.next
    return out


def list_to_tree(vals):
    """把层序数组（含 None 表示空）还原成 TreeNode 二叉树。"""
    if not vals:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    root = TreeNode(vals[0])
    queue = [root]
    i = 1
    while queue and i < len(vals):
        node = queue.pop(0)
        if i < len(vals) and vals[i] is not None:
            node.left = TreeNode(vals[i])
            queue.append(node.left)
        i += 1
        if i < len(vals) and vals[i] is not None:
            node.right = TreeNode(vals[i])
            queue.append(node.right)
        i += 1
    return root


def tree_to_list(root):
    """把 TreeNode 二叉树序列化回层序数组（末尾空节点已裁剪）。"""
    if not root:
        return []
    out = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        if node is None:
            out.append(None)
        else:
            out.append(node.val)
            queue.append(node.left)
            queue.append(node.right)
    while out and out[-1] is None:
        out.pop()
    return out


# ── 注入到 harness 的辅助函数源码 ──
# 不依赖任何外部符号（仅用 INJECT_PROLOGUE 预置的 ListNode / TreeNode / Node）。
HARNESS_STRUCT_SRC = r'''
def _cta_find_node_by_value(root, val):
    """在树中按值查找节点（DFS），用于 p、q 等节点引用参数。

    LeetCode 的树问题（如 LCA）中，p 和 q 是主树中的节点引用，
    不是独立的值。测试用例生成器把 p、q 当成单值传入，需要用
    此函数在主树中查找对应节点。
    """
    if not root:
        return None
    if root.val == val:
        return root
    left = _cta_find_node_by_value(root.left, val)
    if left:
        return left
    return _cta_find_node_by_value(root.right, val)

def _cta_ll_from_list(vals):
    if vals is None:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    dummy = ListNode(0)
    cur = dummy
    for v in vals:
        cur.next = ListNode(v)
        cur = cur.next
    return dummy.next

def _cta_ll_to_list(node):
    out = []
    while node:
        out.append(node.val)
        node = node.next
    return out

def _cta_tree_from_list(vals):
    if not vals:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    root = TreeNode(vals[0])
    queue = [root]
    i = 1
    while queue and i < len(vals):
        node = queue.pop(0)
        if i < len(vals) and vals[i] is not None:
            node.left = TreeNode(vals[i]); queue.append(node.left)
        i += 1
        if i < len(vals) and vals[i] is not None:
            node.right = TreeNode(vals[i]); queue.append(node.right)
        i += 1
    return root

def _cta_tree_to_list(root):
    if not root:
        return []
    out = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        if node is None:
            out.append(None)
        else:
            out.append(node.val)
            queue.append(node.left)
            queue.append(node.right)
    while out and out[-1] is None:
        out.pop()
    return out

def _cta_is_struct_array(type_str: str) -> bool:
    """判断类型是否为「结构体数组」，如 ``List[Optional[ListNode]]``。

    LeetCode 的「合并 K 个升序链表」等题，参数类型是链表**数组**
    （``List[Optional[ListNode]]``），入参形如 ``[[1,4,5],[1,3,4],[2,6]]``：
    每个子数组都要还原成一个独立链表，而不是把整个外层数组当成一个链表。

    判定依据：类型中含 ListNode/TreeNode/Node 且嵌套层数 ≥2（外层 List[...] +
    内层结构体）。单层 ``Optional[ListNode]``（1 个 ``[``）是单结构体，不算数组。
    """
    t = (type_str or "").replace(" ", "")
    if not any(k in t for k in ("ListNode", "TreeNode", "Node")):
        return False
    return t.count("[") >= 2


def _cta_coerce_arg(raw, type_str):
    if not type_str:
        return raw
    t = type_str.replace("Optional[", "").rstrip("]")
    if "ListNode" in t:
        # 链表数组（List[Optional[ListNode]]）：逐个元素各自还原成一条链表
        if _cta_is_struct_array(type_str) and isinstance(raw, list):
            return [_cta_ll_from_list(x) for x in raw]
        return _cta_ll_from_list(raw)
    if "TreeNode" in t or "Node" in t:
        if _cta_is_struct_array(type_str) and isinstance(raw, list):
            return [_cta_tree_from_list(x) for x in raw]
        return _cta_tree_from_list(raw)
    return raw

def _cta_coerce_result(val, type_str):
    if not type_str:
        return val
    t = type_str.replace("Optional[", "").rstrip("]")
    if "ListNode" in t:
        return _cta_ll_to_list(val)
    if "TreeNode" in t or "Node" in t:
        return _cta_tree_to_list(val)
    return val

def _cta_is_void_result(result, return_type):
    """判断是否为「原地修改 / 无返回值」型题目。

    LeetCode 上「合并两个有序数组」「删除排序数组中的重复项」等题目，函数
    返回 ``None``、答案写在首个入参（一般是数组）里。此时应比对 ``args[0]``
    的就地改动，而非返回值。

    触发条件：返回值为 ``None``，且返回类型声明为空（无签名）/ ``None`` / ``void``。
    """
    if result is not None:
        return False
    rt = (return_type or "").strip().lower()
    return rt in ("", "none", "void")
'''
