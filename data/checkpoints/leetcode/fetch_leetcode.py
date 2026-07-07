"""Fetch LeetCode problem via GraphQL and save as Markdown.

Usage: uv run data/checkpoints/leetcode/fetch_leetcode.py <problem-slug>
"""

import sys
import os

from code_tutor_agent.leetcode.leetcode_fetcher import fetch_problem, problem_to_markdown

# Ensure src/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


SLUG = sys.argv[1] if len(sys.argv) > 1 else "two-sum"

p = fetch_problem(SLUG)
md = problem_to_markdown(p)

out_dir = os.path.dirname(__file__)
out_path = os.path.join(out_dir, f"{SLUG}.md")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(md)

print(f"OK {out_path} ({len(md)} chars)")
print(f"Title: {p.title} / {p.difficulty}")
print(f"Tags: {', '.join(p.tags)}")
print(f"Examples: {len(p.examples)}, Constraints: {len(p.constraints)}, Hints: {len(p.hints)}")