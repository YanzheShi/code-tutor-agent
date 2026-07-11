"""35 个 Tag 枚举 —— 覆盖常见算法/数据结构考点。"""
from __future__ import annotations

from enum import Enum


class Tag(str, Enum):
    # array 系（6）
    array_basics = "array_basics"
    array_two_pointers = "array_two_pointers"
    array_sliding_window = "array_sliding_window"
    array_binary_search = "array_binary_search"
    array_prefix_sum = "array_prefix_sum"
    array_sorting = "array_sorting"

    # linkedlist（3）
    linkedlist_basics = "linkedlist_basics"
    linkedlist_two_pointers = "linkedlist_two_pointers"
    linkedlist_cycle = "linkedlist_cycle"

    # stack / queue / heap（4）
    stack_basics = "stack_basics"
    queue_deque = "queue_deque"
    monotonic_stack = "monotonic_stack"
    heap_priority_queue = "heap_priority_queue"

    # tree / graph（7）
    tree_dfs = "tree_dfs"
    tree_bfs = "tree_bfs"
    tree_bst = "tree_bst"
    graph_dfs = "graph_dfs"
    graph_bfs = "graph_bfs"
    graph_topo = "graph_topo"
    union_find = "union_find"

    # DP（4）
    dp_1d = "dp_1d"
    dp_multidim = "dp_multidim"
    dp_interval = "dp_interval"
    dp_tree = "dp_tree"

    # 字符串（3）
    string_basics = "string_basics"
    string_pattern = "string_pattern"
    string_dp = "string_dp"

    # 其他（5）
    backtrack = "backtrack"
    greedy = "greedy"
    bit_manip = "bit_manip"
    math_number_theory = "math_number_theory"
    design = "design"

    @classmethod
    def all_values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def validate(cls, v: str) -> Tag:
        return cls(v)