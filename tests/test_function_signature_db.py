"""Tests for function_signature column persistence in the problems table.

Covers:
  - function_signature is stored and retrieved correctly
  - Backward compatibility: existing problems without the column default to ""
  - LeetCode import path populates function_signature from starter_code
"""
import json
import os
import tempfile

import pytest


class TestFunctionSignaturePersistence:
    """function_signature should survive save → read cycles."""

    @pytest.fixture(autouse=True)
    def _temp_db(self):
        """Each test gets a fresh temp database."""
        from code_tutor_agent.db import database as dbmod
        self._orig_path = dbmod.DB_PATH
        self._tp = os.path.join(tempfile.gettempdir(), f"test_fs_{os.getpid()}.db")
        if os.path.exists(self._tp):
            os.remove(self._tp)
        dbmod.DB_PATH = self._tp
        yield
        dbmod.DB_PATH = self._orig_path
        if os.path.exists(self._tp):
            os.remove(self._tp)

    def test_function_signature_saved_and_retrieved(self):
        """function_signature should survive save → read."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id

        pid, _ = save_problem({
            "title": "test_func_sig",
            "topic": "数组",
            "difficulty": "medium",
            "description": "<p>test</p>",
            "starter_code": "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]: pass",
            "function_signature": "nums:List[int],target:int -> List[int]",
            "test_cases": [],
            "brute_solution": "",
        })
        r = get_problem_by_id(pid)
        assert r is not None
        assert r["function_signature"] == "nums:List[int],target:int -> List[int]"

    def test_function_signature_defaults_empty(self):
        """Problems without function_signature should default to empty string."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id

        pid, _ = save_problem({
            "title": "test_no_func_sig",
            "topic": "数组",
            "difficulty": "easy",
            "description": "<p>test</p>",
            "test_cases": [],
            "brute_solution": "",
        })
        r = get_problem_by_id(pid)
        assert r is not None
        assert r.get("function_signature", "MISSING") == ""

    def test_function_signature_separate_from_starter_code(self):
        """function_signature should be extracted separately from starter_code."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id

        starter = "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]: pass"
        pid, _ = save_problem({
            "title": "test_separate",
            "topic": "数组",
            "difficulty": "medium",
            "description": "<p>test</p>",
            "starter_code": starter,
            "function_signature": "nums:List[int],target:int -> List[int]",
            "test_cases": [],
            "brute_solution": "",
        })
        r = get_problem_by_id(pid)
        assert r["starter_code"] == starter
        assert r["function_signature"] == "nums:List[int],target:int -> List[int]"
        assert r["starter_code"] != r["function_signature"]


class TestLeetCodeImportWithFunctionSignature:
    """The LeetCode import flow should populate function_signature."""

    @pytest.fixture(autouse=True)
    def _temp_db(self):
        from code_tutor_agent.db import database as dbmod
        self._orig_path = dbmod.DB_PATH
        self._tp = os.path.join(tempfile.gettempdir(), f"test_lc_fs_{os.getpid()}.db")
        if os.path.exists(self._tp):
            os.remove(self._tp)
        dbmod.DB_PATH = self._tp
        yield
        dbmod.DB_PATH = self._orig_path
        if os.path.exists(self._tp):
            os.remove(self._tp)

    def test_save_problem_from_leetcode_dict_has_function_signature(self):
        """When saving a LeetCode-style problem_dict, function_signature is persisted."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id

        problem_dict = {
            "title": "Two Sum",
            "topic": "数组",
            "difficulty": "medium",
            "description": "<p>Find two numbers.</p>",
            "starter_code": "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]: pass",
            "function_signature": "nums:List[int],target:int -> List[int]",
            "test_cases": [
                {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"},
            ],
            "brute_solution": "",
        }
        pid, _ = save_problem(problem_dict)
        r = get_problem_by_id(pid)
        assert r is not None
        assert r["function_signature"] == "nums:List[int],target:int -> List[int]"

    def test_visible_test_cases_not_overwritten_by_test_cases_update(self):
        """update_problem_test_cases should only change test_cases_json, not visible_test_cases."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id, update_problem_test_cases

        pid, _ = save_problem({
            "title": "test_visible_preserved",
            "topic": "数组",
            "difficulty": "medium",
            "description": "<p>test</p>",
            "starter_code": "class Solution:\n    def foo(self, x: int) -> int: pass",
            "function_signature": "x:int -> int",
            "test_cases": [
                {"input_args": ["1"], "expected_output": "1", "is_hidden": False},
            ],
            "visible_test_cases": [
                {"input_args": ["1"], "expected_output": "1", "explanation": "original"},
            ],
            "brute_solution": "",
        })

        # Simulate background generation adding more test cases
        update_problem_test_cases(pid, [
            {"input_args": ["1"], "expected_output": "1", "is_hidden": False},
            {"input_args": ["100"], "expected_output": "100", "is_hidden": True},
            {"input_args": ["-5"], "expected_output": "-5", "is_hidden": True},
        ])

        r = get_problem_by_id(pid)
        # visible should be unchanged
        assert len(r["visible_test_cases"]) == 1
        assert r["visible_test_cases"][0]["explanation"] == "original"
        # full test_cases should be updated
        assert len(r["test_cases"]) == 3
