"""随机输入生成器 — 用于暴力解生成测试用例。

Day2 流程：LLM 生成题目 + 参考解后，本模块生成随机合法输入，
用参考解跑出期望输出，产出 (input_args, expected_output) 对作为测试用例。

用法：
    from code_tutor_agent.sandbox.input_generator import generate_random_inputs
    inputs = generate_random_inputs(function_sig="nums: List[int], target: int -> List[int]", count=5)
"""

from __future__ import annotations

import ast
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


_TYPE_GENERATORS = {
    "int": _random_int,
    "float": _random_float,
    "str": _random_str,
    "bool": lambda: random.choice(["True", "False"]),
}


def _generate_param_value(type_str: str) -> str:
    """Generate a random Python literal string for a given type.

    Args:
        type_str: e.g. "int", "List[int]", "List[str]", "List[List[int]]"

    Returns:
        String representation of a random value, e.g. "42", "[1,2,3]"
    """
    type_str = type_str.strip()

    # Handle List[...]
    list_match = re.match(r"List\[(.+)\]", type_str)
    if list_match:
        inner = list_match.group(1)
        inner.strip()
        n = random.randint(1, 5)
        items = [_generate_param_value(inner) for _ in range(n)]
        return "[" + ",".join(items) + "]"

    # Handle List[List[...]]
    list2_match = re.match(r"List\[List\[(.+)\]\]", type_str)
    if list2_match:
        inner = list2_match.group(1)
        n = random.randint(1, 3)
        outer = []
        for _ in range(n):
            inner_n = random.randint(1, 3)
            items = [_generate_param_value(inner) for _ in range(inner_n)]
            outer.append("[" + ",".join(items) + "]")
        return "[" + ",".join(outer) + "]"

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
    seed: int | None = 42,
) -> list[list[str]]:
    """Generate random input argument lists for a function signature.

    Each returned entry is a list of string arguments suitable for
    ``RunnerResult`` (as ``input_args``).

    Args:
        function_sig: e.g. "nums: List[int], target: int -> List[int]"
        count: Number of random inputs to generate.
        constraints: Optional constraint strings (unused for now).
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

    logger.info(
        "▶ generate_random_inputs() — sig=%s, count=%d, params=%s",
        function_sig, count, [p[0] for p in params],
    )

    results = []
    for i in range(count):
        args = []
        for name, type_str in params:
            val = _generate_param_value(type_str)
            args.append(val)
        results.append(args)

    logger.debug("Generated %d random input sets", len(results))
    return results


def _extract_code(text: str) -> str:
    """Strip markdown fences from code."""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()