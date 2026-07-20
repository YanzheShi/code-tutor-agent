"""随机输入生成器 — 用于暴力解生成测试用例。

Day2 流程：LLM 生成题目 + 参考解后，本模块生成随机合法输入，
用参考解跑出期望输出，产出 (input_args, expected_output) 对作为测试用例。

用法：
    from code_tutor_agent.sandbox.input_generator import generate_random_inputs
    inputs = generate_random_inputs(function_sig="nums: List[int], target: int -> List[int]", count=5)
"""

from __future__ import annotations

import ast
import json
import logging
import random
import re
from typing import Any

logger = logging.getLogger(__name__)

# Parse function signature like "nums: List[int], target: int -> List[int]"
# Returns list of (name, type_str) for params, and return_type_str
_SIG_RE = re.compile(r"^(.*?)\s*->\s*(.+)$")
_PARAM_RE = re.compile(r"(\w+)\s*:\s*([^,]+)")


def parse_signature(sig: str) -> tuple[list[tuple[str, str]], str]:
    """Parse a function signature into parameter types and return type.

    Args:
        sig: e.g. "nums: List[int], target: int -> List[int]"

    Returns:
        ( [(name, type), ...], return_type )
    """
    m = _SIG_RE.search(sig)
    if not m:
        logger.warning("Cannot parse signature: %s", sig)
        return [], ""

    params_str = m.group(1).strip()
    return_type = m.group(2).strip()

    params = _PARAM_RE.findall(params_str)
    return params, return_type


def _random_list_int(min_len=2, max_len=8, min_val=-100, max_val=100) -> str:
    """Generate a random List[int] as a string like '[3, 1, 4]'."""
    n = random.randint(min_len, max_len)
    arr = [random.randint(min_val, max_val) for _ in range(n)]
    return "[" + ",".join(str(v) for v in arr) + "]"


def _random_int(min_val=-1000, max_val=1000) -> str:
    return str(random.randint(min_val, max_val))


def _random_float(min_val=-1000.0, max_val=1000.0) -> str:
    return f"{random.uniform(min_val, max_val):.2f}"


def _random_str(max_len=8) -> str:
    n = random.randint(1, max_len)
    chars = "abcdefghijklmnopqrstuvwxyz"
    return '"' + "".join(random.choice(chars) for _ in range(n)) + '"'


def _random_struct_array(type_str: str) -> str:
    """为链表/树类型生成数组形式的随机输入（字符串）。

    链表（ListNode）：``[v1, v2, ...]``
    树（TreeNode/Node）：层序数组，可含 null 表示空子树。
    """
    if type_str in ("TreeNode", "Node"):
        n = random.randint(1, 7)
        vals = []
        for _ in range(n):
            vals.append("null" if random.random() < 0.2 else str(random.randint(-100, 100)))
        if vals[0] == "null":
            vals[0] = str(random.randint(-100, 100))
        return "[" + ",".join(vals) + "]"
    # 默认按链表处理（ListNode / GraphNode 等）
    n = random.randint(1, 6)
    arr = [random.randint(-100, 100) for _ in range(n)]
    return "[" + ",".join(str(v) for v in arr) + "]"


_TYPE_GENERATORS = {
    "int": _random_int,
    "float": _random_float,
    "str": _random_str,
    "bool": lambda: random.choice(["True", "False"]),
}


def _gen_cell(inner: str) -> str:
    """Generate one cell value for a 2D matrix given its element type.

    For ``List[List[str]]`` (grid problems) we emit '0'/'1' single-char
    strings, since grid inputs are almost always flag matrices.
    """
    if inner == "str":
        return '"' + random.choice(["0", "1"]) + '"'
    return _generate_param_value(inner)


def _generate_param_value(type_str: str) -> str:
    """Generate a random Python literal string for a given type.

    Args:
        type_str: e.g. "int", "List[int]", "List[str]", "List[List[int]]",
            "ListNode", "Optional[ListNode]", "TreeNode"

    Returns:
        String representation of a random value, e.g. "42", "[1,2,3]"
    """
    type_str = type_str.strip()

    # 处理 Optional[...] 包装（如 Optional[ListNode]）
    opt_match = re.match(r"^Optional\[(.*)\]$", type_str)
    if opt_match:
        type_str = opt_match.group(1).strip()

    # 链表 / 树结构：以数组形式生成（runner 会按类型还原成对应对象）
    if type_str in ("ListNode", "TreeNode", "Node", "GraphNode"):
        return _random_struct_array(type_str)

    # Handle 2D List[List[...]] FIRST — more specific than the 1D pattern.
    # (A greedy 1D regex would otherwise swallow "List[List[str]]" whole,
    #  turning it into a ragged list-of-lists of random strings.)
    m2 = re.match(r"^List\[List\[(.+?)\]\]$", type_str)
    if m2:
        inner = m2.group(1).strip()
        rows = random.randint(1, 4)
        cols = random.randint(1, 5)
        matrix = [
            "[" + ",".join(_gen_cell(inner) for _ in range(cols)) + "]"
            for _ in range(rows)
        ]
        return "[" + ",".join(matrix) + "]"

    # Handle 1D List[...]
    m1 = re.match(r"^List\[(.+)\]$", type_str)
    if m1:
        inner = m1.group(1).strip()
        n = random.randint(1, 6)
        items = [_generate_param_value(inner) for _ in range(n)]
        return "[" + ",".join(items) + "]"

    # Handle basic types
    gen = _TYPE_GENERATORS.get(type_str)
    if gen:
        return gen()

    # Fallback: return a default
    logger.warning("Unknown type '%s', defaulting to int", type_str)
    return _random_int()


def generate_random_inputs(
    function_sig: str,
    count: int = 3,
    constraints: list[str] | None = None,
    description: str | None = None,
    seed: int | None = 42,
) -> list[list[str]]:
    """Generate random input argument lists for a function signature.

    Each returned entry is a list of string arguments suitable for
    ``RunnerResult`` (as ``input_args``).

    Args:
        function_sig: e.g. "nums: List[int], target: int -> List[int]"
        count: Number of random inputs to generate.
        constraints: Optional constraint strings；若含「有序/sorted/非递减」等
            关键词，会对 List[int] 输入做升序排序（方案 B）。
        description: Optional problem description，同样参与「有序」判定。
        seed: Random seed for reproducibility.

    Returns:
        List of input_args lists, e.g. [["[1,2,3]", "5"], ["[4,5]", "9"]]
    """
    if seed is not None:
        random.seed(seed)

    params, _ = parse_signature(function_sig)
    if not params:
        logger.warning("Empty params from sig '%s', returning empty", function_sig)
        return []

    # 综合 constraints + description 判断是否为「有序输入」类题目。
    sort_inputs = _needs_sorted_inputs(*(constraints or []), description or "")

    logger.info(
        "▶ generate_random_inputs() — sig=%s, count=%d, sort_inputs=%s, params=%s",
        function_sig, count, sort_inputs, [p[0] for p in params],
    )

    results = []
    for i in range(count):
        args = []
        prev_type = None
        prev_val = None
        for name, type_str in params:
            t = type_str.strip()
            # 位置/倒数索引类参数（如「倒数第 k 个」的 k）：若前一个参数是
            # 结构类型（链表/树），则把 k 约束在 [1, len(结构)]，避免生成非法负值。
            if t == "int" and prev_type in ("ListNode", "TreeNode", "Node", "GraphNode") and prev_val:
                try:
                    _lst = ast.literal_eval(prev_val)
                    _n = len(_lst) if isinstance(_lst, list) else 1
                except Exception:
                    _n = 5
                val = str(random.randint(1, max(1, _n)))
            # 数组后的「长度」参数（m/n/len/size/k…）：约束为该数组的真实长度，
            # 而不是随机整数。这是「合并两个有序数组」等题 m/n 乱数的根因修复。
            elif t == "int" and prev_type and _is_list_type(prev_type) and _looks_like_length(name) and prev_val:
                try:
                    _lst = ast.literal_eval(prev_val)
                    _n = len(_lst) if isinstance(_lst, list) else 1
                except Exception:
                    _n = 5
                val = str(_n)
            else:
                val = _generate_param_value(type_str)
            args.append(val)
            prev_type = t
            prev_val = val
        # 有序类题目：对 List[int] 输入的「真实元素」升序排序（方案 B）。
        # 在 m/n 长度参数已确定之后统一排序，由 sort_sorted_inputs 保证
        # 只排前 m/n 个真实元素、保留补零、不动 m/n。
        if sort_inputs:
            args = sort_sorted_inputs(function_sig, args)
        results.append(args)

    logger.debug("Generated %d random input sets", len(results))
    return results


def _extract_code(text: str) -> str:
    """Strip markdown fences from code."""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()


# 提示某 int 参数是「数组长度」的命名集合（合并两个有序数组的 m/n 等都命中）
_LENGTH_HINTS = {"m", "n", "k", "l", "len", "length", "size"}


def _looks_like_length(name: str) -> bool:
    n = name.lower().strip()
    if n in _LENGTH_HINTS:
        return True
    return any(h in n for h in ("len", "length", "size", "count"))


def _is_list_type(type_str: str) -> bool:
    return bool(re.match(r"^List\[", type_str.strip()))


# 提示题目「输入数组应当有序」的关键词（合并有序数组 / 有序数组二分等）。
# 命中后会对 List[int] 随机输入做升序排序，保证语义正确（方案 B）。
_SORTED_HINTS = (
    "non-decreasing", "non-increasing", "sorted in", "ascending order",
    "descending order", "升序", "降序", "有序", "非递减", "非递增",
    "sorted array", "sorted list",
)


def _needs_sorted_inputs(*texts: str) -> bool:
    """题目是否要求输入数组「有序」。

    综合 constraints + description 文本判断，命中任一关键词即认为需要。
    这是「合并两个有序数组」随机用例输入无序的根因修复（方案 B）。
    """
    blob = " \n ".join(t for t in texts if t).lower()
    return any(h in blob for h in _SORTED_HINTS)


def _is_list_of_ints(type_str: str) -> bool:
    """仅匹配 1D 的 List[int]，不匹配 List[List[int]] 等。"""
    return re.match(r"^List\[int\]$", type_str.strip()) is not None


def sort_sorted_inputs(func_sig: str, input_args: list[str]) -> list[str]:
    """「有序」类题目：对 List[int] 输入的「真实元素」升序排序，保留补零。

    与粗暴地对整个数组排序不同，这里按各自的「长度参数」(m/n/len/k…) 只排序
    前 ``length`` 个真实元素，尾部的补零保持不动；没有长度配对（如单有序数组）
    则整体排序。不会改动 m/n 本身。

    例：``['[1,0,0,0,0]','3','[2,3]','2']`` -> ``['[0,0,1,0,0]','3','[2,3]','2']``
    """
    params, _ = parse_signature(func_sig)
    # 为每个 List[int] 找紧随其后的「长度」参数做配对
    pairs: list[tuple[int, int]] = []
    for i, (name, t) in enumerate(params):
        if _is_list_of_ints(t):
            for j in range(i + 1, len(params)):
                if params[j][1].strip() == "int" and _looks_like_length(params[j][0]):
                    pairs.append((i, j))
                    break
    values = [ast.literal_eval(a) for a in input_args]
    used = set()
    for li, ln in pairs:
        used.add(li)
        if isinstance(values[li], list) and ln < len(values) and isinstance(values[ln], int):
            length = values[ln]
            values[li] = sorted(values[li][:length]) + values[li][length:]
    # 无长度配对的 List[int]（如单有序数组 / 二分查找的 nums）：整体排序
    for i, (name, t) in enumerate(params):
        if _is_list_of_ints(t) and i not in used and isinstance(values[i], list):
            values[i] = sorted(values[i])
    # 还原成与 input_args 同格式（同 _to_arg_str）的字符串，保持列表带空格分隔
    return [json.dumps(v, ensure_ascii=False) if isinstance(v, list) else str(v)
            for v in values]


def _to_arg_str(v: Any) -> str:
    """把 Python 值还原回 input_args 用的 JSON 字面量字符串。"""
    if isinstance(v, str):
        return '"' + v + '"'
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def sanitize_test_case(func_sig: str, tc: dict, sort_inputs: bool = False) -> dict | None:
    """校验并校正一条测试用例的 ``input_args``，使其满足函数签名契约。

    解决两类由随机/LLM 生成器引入、导致判题误判的常见错误：

    1. 数组长度参数（m/n/len/size/k…）被生成成随机整数
       → 重算为紧随其后的真实数组长度（仅在数组尚未补零时，避免覆盖
       合并类已补零输入里 LLM 声明的 ``m``）；
    2. 双数组合并类（如「合并两个有序数组」）：第一个数组需原地容纳两数组，
       长度应为各数组长度之和，因此对第一个数组补零到 ``m + n``。

    当 ``sort_inputs=True``（「有序」类题目，见方案 B），会对各 ``List[int]``
    输入按各自长度参数排序「真实元素」、保留尾部补零。由于 expected 由参考解
    在同份 input 上跑出，排序只让输入更贴近题面语义，不影响判题 Pass。

    返回校正后的 ``tc``（就地修改后返回）；若 ``input_args`` 无法解析或参数个数
    与签名不匹配，返回 ``None``（调用方应丢弃该用例）。
    """
    if not func_sig:
        return tc
    params, _ = parse_signature(func_sig)
    raw_args = tc.get("input_args") or []
    if not params or len(raw_args) != len(params):
        # 参数个数对不上，无法安全校正 -> 交给调用方丢弃
        return None

    try:
        values = [ast.literal_eval(a) for a in raw_args]
    except Exception:
        return None

    # 找出 (List 参数下标, 紧随的长度参数下标) 对
    length_pairs: list[tuple[int, int]] = []
    for j, (name, t) in enumerate(params):
        if j + 1 < len(params) and _is_list_type(t) and params[j + 1][1].strip() == "int":
            if _looks_like_length(params[j + 1][0]):
                length_pairs.append((j, j + 1))

    if not length_pairs:
        return tc  # 无数组长度参数，无需校正

    # 重算每个长度参数为真实数组长度。
    # 仅当数组「已补零」（声明长度为正 且 数组长度 > 声明长度，即 len==m+n>m）
    # 时才保留 LLM 声明的 m/n，避免把它改坏；其余情况（乱数/负数/未补零）一律
    # 以真实数组长度覆盖。
    for list_idx, len_idx in length_pairs:
        if isinstance(values[list_idx], list):
            arr = values[list_idx]
            declared = values[len_idx]
            if not (isinstance(declared, int) and declared > 0 and len(arr) > declared):
                values[len_idx] = len(arr)

    # 合并类：存在 ≥2 个 (List, 长度) 对 -> 第一个数组补零到各长度之和
    if len(length_pairs) >= 2:
        first_list_idx = length_pairs[0][0]
        total = sum(
            values[len_idx]
            for _, len_idx in length_pairs
            if isinstance(values[len_idx], int)
        )
        lst = values[first_list_idx]
        if isinstance(lst, list) and 0 <= len(lst) < total:
            values[first_list_idx] = lst + [0] * (total - len(lst))

    arg_strs = [_to_arg_str(v) for v in values]
    # 「有序」类题目：对 List[int] 真实元素升序排序（方案 B），保留补零。
    if sort_inputs:
        arg_strs = sort_sorted_inputs(func_sig, arg_strs)
    tc["input_args"] = arg_strs
    return tc