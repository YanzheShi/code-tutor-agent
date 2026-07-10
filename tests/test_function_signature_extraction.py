"""Tests for extract_function_signature from starter_code.

Covers:
  - Typical LeetCode method signatures with type annotations
  - Return type inference (explicit and defaulting to None)
  - Edge cases: empty string, no method, parameters with defaults
"""
import pytest

from code_tutor_agent.leetcode.leetcode_fetcher import extract_function_signature


class TestExtractFunctionSignature:

    def test_two_sum_signature(self):
        starter = """class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        pass
"""
        result = extract_function_signature(starter)
        assert result == "nums:List[int],target:int -> List[int]"

    def test_single_param_no_return_annotation(self):
        starter = """class Solution:
    def lengthOfLongestSubstring(self, s: str) -> int:
        pass
"""
        result = extract_function_signature(starter)
        assert result == "s:str -> int"

    def test_void_return(self):
        starter = """class Solution:
    def solve(self, matrix: List[List[int]]) -> None:
        pass
"""
        result = extract_function_signature(starter)
        assert result == "matrix:List[List[int]] -> None"

    def test_multiple_params(self):
        starter = """class Solution:
    def merge(self, nums1: List[int], m: int, nums2: List[int], n: int) -> None:
        pass
"""
        result = extract_function_signature(starter)
        assert result == "nums1:List[int],m:int,nums2:List[int],n:int -> None"

    def test_empty_starter_code(self):
        assert extract_function_signature("") == ""

    def test_no_method_definition(self):
        starter = "class Solution:\n    pass"
        assert extract_function_signature(starter) == ""

    def test_params_with_default_values(self):
        starter = """class Solution:
    def method(self, a: int, b: str = "hello", c: List[int] = None) -> bool:
        pass
"""
        result = extract_function_signature(starter)
        # Should strip default values from parameter types
        assert "a:int" in result
        assert "b:str" in result
        assert "c:List[int]" in result
        assert "-> bool" in result
        # Should not contain default value expressions
        assert '"hello"' not in result
        assert "= None" not in result

    def test_no_params_only_self(self):
        starter = """class Solution:
    def method(self) -> int:
        pass
"""
        result = extract_function_signature(starter)
        assert result == "-> int"

    def test_nested_generic_types(self):
        starter = """class Solution:
    def func(self, matrix: List[List[str]]) -> List[List[str]]:
        pass
"""
        result = extract_function_signature(starter)
        assert "matrix:List[List[str]]" in result
        # The return type is captured up to end of line
        assert "List[List[str]]" in result
