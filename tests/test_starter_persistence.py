"""Test: starter_code and visible_test_cases persistence."""
import json
import os
import tempfile

import pytest


class TestStarterCodePersistence:

    def test_starter_code_saved_and_retrieved(self):
        """starter_code should survive save -> read cycle."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id
        import code_tutor_agent.db.database as dbmod
        original = dbmod.DB_PATH
        tp = os.path.join(tempfile.gettempdir(), f"test_st_{os.getpid()}.db")
        dbmod.DB_PATH = tp
        try:
            pid = save_problem({
                "title": "test_starter_persist",
                "topic": "数组", "difficulty": "easy",
                "description": "<p>test</p>",
                "starter_code": "class Solution:\n    def test(self): pass",
                "test_cases": [], "brute_solution": "",
            })
            r = get_problem_by_id(pid)
            assert r.get("starter_code") == "class Solution:\n    def test(self): pass"
        finally:
            dbmod.DB_PATH = original
            if os.path.exists(tp): os.remove(tp)

    def test_starter_code_defaults_empty(self):
        """Problems without starter_code should return empty string."""
        from code_tutor_agent.db.database import save_problem, get_problem_by_id
        import code_tutor_agent.db.database as dbmod
        original = dbmod.DB_PATH
        tp = os.path.join(tempfile.gettempdir(), f"test_st2_{os.getpid()}.db")
        dbmod.DB_PATH = tp
        try:
            pid = save_problem({
                "title": "test_starter_empty",
                "topic": "数组", "difficulty": "easy",
                "description": "<p>test</p>",
                "test_cases": [], "brute_solution": "",
            })
            r = get_problem_by_id(pid)
            assert r.get("starter_code", "MISSING") == ""
        finally:
            dbmod.DB_PATH = original
            if os.path.exists(tp): os.remove(tp)


class TestTestCaseSeparation:

    def _setup(self):
        """Create a temp DB and return the module with overridden path."""
        from code_tutor_agent.db import database as dbmod
        original_path = dbmod.DB_PATH
        test_path = os.path.join(tempfile.gettempdir(), f"test_tc_sep_{os.getpid()}.db")
        if os.path.exists(test_path):
            os.remove(test_path)
        dbmod.DB_PATH = test_path
        return dbmod, original_path, test_path

    def _teardown(self, dbmod, original_path, test_path):
        dbmod.DB_PATH = original_path
        if os.path.exists(test_path):
            os.remove(test_path)

    def test_visible_and_full_stored_separately(self):
        """Visible test cases should be stored independently from full test cases."""
        dbmod, orig, tpath = self._setup()
        try:
            pid = dbmod.save_problem({
                "title": "test_sep_tcs",
                "topic": "数组",
                "difficulty": "easy",
                "description": "<p>test</p>",
                "starter_code": "class Solution:\n    def test(self): pass",
                "test_cases": [{"input_args": ["1"], "expected_output": "1", "is_hidden": True}],
                "visible_test_cases": [
                    {"input_args": ["1"], "expected_output": "1", "explanation": "visible only"},
                ],
                "brute_solution": "",
            })
            assert pid > 0

            # Verify after save: both columns exist and are separate
            result = dbmod.get_problem_by_id(pid)
            assert result is not None
            assert len(result["visible_test_cases"]) == 1
            assert result["visible_test_cases"][0]["explanation"] == "visible only"
            assert len(result["test_cases"]) == 1
            assert result["test_cases"][0]["is_hidden"] == True

            # Simulate background gen updating full test_cases only
            dbmod.update_problem_test_cases(pid, [
                {"input_args": ["1"], "expected_output": "1", "is_hidden": False},
                {"input_args": ["2"], "expected_output": "2", "is_hidden": True},
                {"input_args": ["3"], "expected_output": "3", "is_hidden": True},
            ])

            # Verify: visible_test_cases unchanged, test_cases updated
            result2 = dbmod.get_problem_by_id(pid)
            assert len(result2["visible_test_cases"]) == 1  # unchanged
            assert result2["visible_test_cases"][0]["explanation"] == "visible only"
            assert len(result2["test_cases"]) == 3  # updated
            assert result2["test_cases"][0]["is_hidden"] == False
            assert result2["test_cases"][2]["is_hidden"] == True

        finally:
            self._teardown(dbmod, orig, tpath)

    def test_visible_fallback_to_test_cases(self):
        """When no visible_test_cases provided, fallback to test_cases."""
        dbmod, orig, tpath = self._setup()
        try:
            pid = dbmod.save_problem({
                "title": "test_fallback_tcs",
                "topic": "数组",
                "difficulty": "easy",
                "description": "<p>test</p>",
                "test_cases": [
                    {"input_args": ["1"], "expected_output": "1", "is_hidden": False},
                    {"input_args": ["2"], "expected_output": "2", "is_hidden": True},
                ],
                "brute_solution": "",
            })
            result = dbmod.get_problem_by_id(pid)
            assert len(result["visible_test_cases"]) == 2  # fallback from test_cases
            assert len(result["test_cases"]) == 2
        finally:
            self._teardown(dbmod, orig, tpath)