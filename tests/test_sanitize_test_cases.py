"""sanitize_test_case 校验：修复 LLM/随机生成器引入的 m/n 乱数、数组长度不符等。

回归用例对应「合并两个有序数组」（双 List + 各自长度参数）坏输入导致判题 RE 的问题。
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from code_tutor_agent.sandbox.input_generator import sanitize_test_case
from code_tutor_agent.sandbox.runner import run_solution

_MERGE_SIG = "nums1: List[int], m: int, nums2: List[int], n: int -> None"
_MERGE_CODE = """
from typing import List
class Solution:
    def merge(self, nums1, m, nums2, n):
        for i in range(len(nums1) - 1, -1, -1):
            if (nums1[m - 1] if m > 0 else -10 ** 10) > (nums2[n - 1] if n > 0 else -10 ** 10):
                nums1[i] = nums1[m - 1]; m -= 1
            else:
                nums1[i] = nums2[n - 1]; n -= 1
"""


def test_merge_sanitize_fixes_mn_and_pads():
    tc = {"input_args": ["[1,2,3]", "643", "[2,5,6]", "-759"]}
    out = sanitize_test_case(_MERGE_SIG, tc)
    assert out is not None
    args = out["input_args"]
    assert args[0] == "[1, 2, 3, 0, 0, 0]", args  # 补零到 m+n=6
    assert args[1] == "3"                         # m 重算为 nums1 真实长度
    assert args[2] == "[2, 5, 6]"                 # nums2 不变
    assert args[3] == "3"                         # n 重算为 nums2 真实长度


def test_merge_sanitized_input_runs_passed():
    tc = {"input_args": ["[1,2,3]", "643", "[2,5,6]", "-759"]}
    out = sanitize_test_case(_MERGE_SIG, tc)
    out["expected_output"] = ""  # run_solution 要求 tc 带 expected_output 键
    results = run_solution(_MERGE_CODE, [out], timeout=10.0, function_signature=_MERGE_SIG)
    # 参考解在校正后 input 上不应崩溃（status 非 RE），且产出正确合并结果
    assert results[0].status != "Runtime Error", results[0].detail
    assert results[0].detail == "[1,2,2,3,5,6]", results[0].detail


def test_unsanitized_bad_input_causes_re_but_sanitized_passes():
    """对比：未经校正的乱数 m/n 会让参考解 RE；校正后立即正常。"""
    bad = {"input_args": ["[1,2,3]", "643", "[2,5,6]", "-759"], "expected_output": ""}
    bad_res = run_solution(_MERGE_CODE, [bad], timeout=10.0, function_signature=_MERGE_SIG)[0]
    assert bad_res.status == "Runtime Error", bad_res.detail  # 证明坏 input 会崩

    good = sanitize_test_case(_MERGE_SIG, {"input_args": ["[1,2,3]", "643", "[2,5,6]", "-759"]})
    good["expected_output"] = ""
    good_res = run_solution(_MERGE_CODE, [good], timeout=10.0, function_signature=_MERGE_SIG)[0]
    assert good_res.status != "Runtime Error", good_res.detail
    assert good_res.detail == "[1,2,2,3,5,6]", good_res.detail


def test_generate_complex_tests_sanitizes_boundary_end_to_end():
    """端到端（mock LLM/DB）：生成管线收到 m/n 乱数的边界用例，应自动校正并保留。"""
    from code_tutor_agent.api.services.generation import _generate_complex_tests
    from code_tutor_agent.db import database as db_mod
    from code_tutor_agent.config import get_llm

    full = SimpleNamespace(
        optimal_solution=_MERGE_CODE,
        brute_solution="",
        function_signature=_MERGE_SIG,
        title="合并两个有序数组",
        description="合并两个有序数组为一个有序数组",
        difficulty="Easy",
        constraints=["nums1 长度为 m，nums2 长度为 n"],
        test_cases=[],
    )
    bad_llm_json = json.dumps([{
        "input_args": ["[1,2,3]", "643", "[2,5,6]", "-759"],
        "expected_output": "",
        "explanation": "bad boundary from llm",
    }])

    class _Resp:
        content = bad_llm_json

    class _LLM:
        def invoke(self, msgs):
            return _Resp()

    captured = {}

    def fake_get_problem(pid):
        return full

    def fake_update(pid, suite, visible_final=None):
        captured["suite"] = suite
        captured["visible_final"] = visible_final

    with patch.object(db_mod, "get_problem_by_id", fake_get_problem), \
         patch.object(db_mod, "update_problem_test_cases", fake_update), \
         patch("code_tutor_agent.config.get_llm", return_value=_LLM()):
        # _generate_complex_tests 是同步函数，线上用 asyncio.to_thread 调用；
        # 单测直接同步调用即可（不要 asyncio.run，否则会报 None 非协程）。
        _generate_complex_tests(999003, "sid-test")

    suite = captured.get("suite", [])
    bc = next((t for t in suite if t.get("explanation") == "bad boundary from llm"), None)
    assert bc is not None, "坏边界用例应经 sanitize 后保留"
    args = bc["input_args"]
    assert args[1] == "3" and args[3] == "3", args       # m/n 已重算
    assert args[0] == "[1, 2, 3, 0, 0, 0]", args         # nums1 已补零到 m+n


def test_remove_duplicates_single_list_no_pad():
    sig = "nums: List[int], n: int -> int"
    tc = {"input_args": ["[1,1,2]", "999"]}
    out = sanitize_test_case(sig, tc)
    assert out is not None
    args = out["input_args"]
    assert args[0] == "[1, 1, 2]"   # 单 List 不补零
    assert args[1] == "3"           # n 重算为真实长度


def test_no_length_param_passthrough():
    sig = "nums: List[int], target: int -> List[int]"
    tc = {"input_args": ["[2,7,11,15]", "9"]}
    out = sanitize_test_case(sig, tc)
    assert out is not None
    assert out["input_args"] == ["[2,7,11,15]", "9"]  # 无长度参数，原样返回


def test_malformed_returns_none():
    sig = "nums1: List[int], m: int, nums2: List[int], n: int -> None"
    # 参数个数不匹配
    assert sanitize_test_case(sig, {"input_args": ["[1,2,3]", "3"]}) is None
    # 某 input_args 无法解析
    assert sanitize_test_case(sig, {"input_args": ["[1,2,3", "3", "[2]", "1"]}) is None


# ── LeetCode 示例变量名前缀剥离（修复「Sample/visible case input malformed,
#    dropping」导致示例用例被整条丢弃的 bug）──


def test_kw_prefix_equals_single_list():
    """Input 写成 ``nums = [-7,-3,2,3,11]`` 这类带 = 前缀的示例应被正确解析。"""
    sig = "nums: List[int] -> List[int]"
    out = sanitize_test_case(sig, {"input_args": ["nums = [-7,-3,2,3,11]"]})
    assert out is not None, "带 = 前缀的示例用例不应被丢弃"
    assert out["input_args"] == ["[-7, -3, 2, 3, 11]"], out["input_args"]


def test_kw_prefix_colon_single_list():
    """``nums: [-7,-3,2,3,11]`` 这种 : 前缀也应被剥离。"""
    sig = "nums: List[int] -> List[int]"
    out = sanitize_test_case(sig, {"input_args": ["nums: [-7,-3,2,3,11]"]})
    assert out is not None
    assert out["input_args"] == ["[-7, -3, 2, 3, 11]"], out["input_args"]


def test_kw_prefix_multi_param_merge():
    """多参数示例每个元素都带前缀，应逐个剥离并保留 m/n 校正逻辑。"""
    sig = "nums1: List[int], m: int, nums2: List[int], n: int -> None"
    out = sanitize_test_case(
        sig,
        {"input_args": ["nums1 = [1,2,3]", "m = 3", "nums2 = [4,5,6]", "n = 3"]},
    )
    assert out is not None, "多参数前缀示例不应被丢弃"
    args = out["input_args"]
    assert args[0] == "[1, 2, 3, 0, 0, 0]", args  # nums1 补零到 m+n=6
    assert args[1] == "3"                          # m 重算
    assert args[2] == "[4, 5, 6]"
    assert args[3] == "3"                          # n 重算


def test_kw_prefix_survives_and_runs():
    """端到端：带 = 前缀的示例剥离后，参考解能正常跑出正确结果（不被丢）。"""
    sig = "nums: List[int] -> List[int]"
    code = (
        "from typing import List\n"
        "class Solution:\n"
        "    def sortedSquares(self, nums):\n"
        "        return sorted(x * x for x in nums)\n"
    )
    tc = {"input_args": ["nums = [-7,-3,2,3,11]"], "expected_output": ""}
    out = sanitize_test_case(sig, tc)
    assert out is not None
    results = run_solution(code, [out], timeout=10.0, function_signature=sig)
    assert results[0].status != "Runtime Error", results[0].detail
    # 参考解真实输出应覆盖 LLM 可能编错的期望（题目 15 曾编成 [4,9,16,49,121]）
    assert results[0].detail.replace(" ", "") == "[4,9,9,49,121]", results[0].detail

