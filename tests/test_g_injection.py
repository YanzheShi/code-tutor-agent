"""G 维度注入：只给一个方向 + 输出预算（2026-11-21）。

当天滑动窗口一次注入 8 条方向，模型写满 8192 token 被截断；重试时又照抄
「找所有异位词」出了 LC 438。本用例锁住：
- 维度仍随机选 1 个，但该维度只注入 1 条方向；
- retry hint 与 system prompt 都带输出预算。
"""

from __future__ import annotations

import random

from code_tutor_agent.generation import injection
from code_tutor_agent.generation.problem_generation_agent import _selfcheck_retry_hint
from code_tutor_agent.prompts.generate_problem import GENERATE_PROBLEM_SYSTEM


def test_g_suffix_injects_exactly_one_direction(monkeypatch):
    dims = [{
        "dimension_name": "测试维度",
        "description": "测试思路",
        "example_directions": ["方向A", "方向B", "方向C"],
    }]
    monkeypatch.setattr(injection, "load_dimensions", lambda topic: dims)

    suffix = injection.build_g_suffix(random.Random(0), "测试")

    assert suffix is not None
    assert suffix.count("    · ") == 1
    assert "只围绕下面这一个具体方向" in suffix
    assert len([d for d in dims[0]["example_directions"] if d in suffix]) == 1


def test_real_dimension_file_injects_one_direction():
    suffix = injection.build_g_suffix(random.Random(0), "滑动窗口")
    assert suffix is not None
    assert suffix.count("    · ") == 1


def test_g_suffix_without_directions_has_no_bullet(monkeypatch):
    dims = [{"dimension_name": "d", "description": "x", "example_directions": []}]
    monkeypatch.setattr(injection, "load_dimensions", lambda topic: dims)

    suffix = injection.build_g_suffix(random.Random(0), "测试")

    assert suffix is not None
    assert "·" not in suffix


def test_retry_hint_includes_output_budget():
    hint = _selfcheck_retry_hint("数组")
    assert "输出预算" in hint
    assert "examples 最多 2 个" in hint
    assert "alternative_solutions" in hint
    assert "description ≤ 300 字" in hint


def test_system_prompt_includes_output_budget():
    assert "输出预算（硬约束）" in GENERATE_PROBLEM_SYSTEM
    assert "`alternative_solutions` 一律 `[]`" in GENERATE_PROBLEM_SYSTEM
