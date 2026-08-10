"""LeetCodeGateway — LeetCode 拉题薄封装（设计 §8）。

底层：``fetch_problem_list`` / ``fetch_problem`` / ``problem_to_api_dict`` /
``_parse_examples_to_test_cases``（leetcode_fetcher，默认 leetcode.cn 抓取，
列表为 leetcode.com 能力）。
"""

from __future__ import annotations

import logging
import re

from code_tutor_agent.leetcode.leetcode_fetcher import (
    _parse_examples_to_test_cases,
    extract_function_signature,
    fetch_problem,
    fetch_problem_list,
    problem_to_api_dict,
)

logger = logging.getLogger(__name__)

# 口语 topic → LeetCode 主题 slug（fetch_problem_list 的 filters.tags 语义）
_TOPIC_LC_SLUGS: dict[str, str] = {
    "数组": "array",
    "数组+哈希表": "hash-table",
    "双指针": "two-pointers",
    "滑动窗口": "sliding-window",
    "二分查找": "binary-search",
    "链表": "linked-list",
    "栈": "stack",
    "队列": "queue",
    "动态规划": "dynamic-programming",
    "字符串": "string",
    "递归": "recursion",
    "回溯": "backtracking",
    "贪心": "greedy",
    "位运算": "bit-manipulation",
    "排序": "sorting",
    "前缀和": "prefix-sum",
    "图": "graph",
    "图论": "graph",
    "图遍历": "graph",
    "图的dfs": "depth-first-search",
    "图的bfs": "breadth-first-search",
    "拓扑排序": "topological-sort",
    "最短路径": "shortest-path",
    "并查集": "union-find",
    "树": "tree",
    "树结构": "tree",
    "二叉树": "binary-tree",
    "线段树": "segment-tree",
    "堆": "heap-priority-queue",
    "优先队列": "heap-priority-queue",
    "跳表": "design",
    "数论": "math",
}


class LeetCodeGateway:
    def list(self, topic: str, difficulty: str | None = None, limit: int = 10) -> list[str]:
        """按主题+难度拉题，返回题目 slug 列表（排除付费题）。"""
        slug = _TOPIC_LC_SLUGS.get(topic, "") or topic
        diff = difficulty.upper() if difficulty else None
        result = fetch_problem_list(slug, difficulty=diff, limit=limit)
        return [item.slug for item in result.items if not item.paid_only]

    def fetch(self, slug: str) -> dict:
        """按 slug 抓题并转成 to_lc_dict 产物（含 parsed_test_cases）。"""
        return problem_to_api_dict(fetch_problem(slug))

    def to_lc_dict(self, problem) -> dict:
        """LeetCodeProblem 对象 → API dict。"""
        return problem_to_api_dict(problem)

    def parse_examples(self, examples: list[str], starter_code: str) -> list[dict]:
        """示例文本 → 测试用例（带函数签名推断）。"""
        return _parse_examples_to_test_cases(examples, starter_code)

    def extract_signature(self, starter_code: str) -> str:
        """从 LeetCode 风格 starter_code 提取函数签名。"""
        return extract_function_signature(starter_code)


def slug_from_url(url: str) -> str | None:
    """从 LeetCode 题目 URL 提取 slug；非合法 URL 返回 None。"""
    if not url or not isinstance(url, str):
        return None
    match = re.search(r"/problems/([^/?#]+)", url)
    return match.group(1) if match else None
