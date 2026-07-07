"""PoC: Fetch LeetCode official solution.

Usage:
    uv run python -m pytest tests/leetcode/test_leetcode_solution_fetcher.py -v

    # or run directly:
    PYTHONPATH=src python tests/leetcode/test_leetcode_solution_fetcher.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src is on path when run directly
SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(SRC))

from code_tutor_agent.leetcode.leetcode_solution_fetcher import (
    fetch_official_solution,
    fetch_solution_list,
    fetch_solution_detail,
)


def test_official_solution_cn():
    """Official solution from leetcode.cn."""
    sol = fetch_official_solution("two-sum", "leetcode.cn")
    assert sol is not None, "two-sum should have an official solution on leetcode.cn"
    assert sol["title"] == "两数之和"
    assert len(sol["content_html"]) > 100
    assert sol["can_see_detail"] is True
    print(f"  ✅ official solution (cn): {sol['title']} ({len(sol['content_html'])} chars)")


def test_official_solution_com():
    """Official solution from leetcode.com."""
    sol = fetch_official_solution("two-sum", "leetcode.com")
    assert sol is not None, "two-sum should have an official solution on leetcode.com"
    assert sol["title"] == "Two Sum"
    assert len(sol["content_html"]) > 100
    print(f"  ✅ official solution (com): {sol['title']} ({len(sol['content_html'])} chars)")


def test_official_solution_no_such_problem():
    """Non-existent problem should return None."""
    sol = fetch_official_solution("this-problem-does-not-exist-12345", "leetcode.cn")
    assert sol is None


# ── Run directly ──

if __name__ == "__main__":
    test_official_solution_cn()
    test_official_solution_com()
    test_official_solution_no_such_problem()
    print("\nAll PoC tests passed.")