"""judge0_client._py38_compat_source 单测：3.8 内建泛型注解兼容前置。"""
from __future__ import annotations

from code_tutor_agent.sandbox.judge0_client import (
    JUDGE0_PYTHON_ID,
    _py38_compat_source,
)

FUTURE = "from __future__ import annotations"

CODE_BUILTIN = (
    "class Solution:\n"
    "    def solve(self, items: list[str]) -> dict[str, int]:\n"
    "        return {}\n"
)
CODE_TYPING = (
    "from typing import List\n"
    "class Solution:\n"
    "    def solve(self, items: List[str]) -> int:\n"
    "        return 0\n"
)
CODE_PLAIN = (
    "class Solution:\n"
    "    def solve(self, nums):\n"
    "        return sum(nums)\n"
)


def test_prepends_future_import_for_builtin_generics():
    out = _py38_compat_source(CODE_BUILTIN, JUDGE0_PYTHON_ID)
    assert out.startswith(FUTURE + "\n")
    assert "list[str]" in out  # 原代码内容保留


def test_noop_for_typing_style():
    assert _py38_compat_source(CODE_TYPING, JUDGE0_PYTHON_ID) == CODE_TYPING


def test_noop_for_plain_code():
    assert _py38_compat_source(CODE_PLAIN, JUDGE0_PYTHON_ID) == CODE_PLAIN


def test_noop_when_future_import_already_present():
    src = FUTURE + "\n" + CODE_BUILTIN
    assert _py38_compat_source(src, JUDGE0_PYTHON_ID) == src


def test_noop_for_other_language():
    assert _py38_compat_source(CODE_BUILTIN, 70) == CODE_BUILTIN


def test_string_literal_false_positive_is_harmless():
    """源码里字符串含 'list[' 会误判前置——无副作用（多一行 import）。"""
    src = 'def f():\n    return "list[abc]"\n'
    out = _py38_compat_source(src, JUDGE0_PYTHON_ID)
    assert out.startswith(FUTURE + "\n")
    assert 'return "list[abc]"' in out
