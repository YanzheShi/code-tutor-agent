"""共享题目解析（DP-5）：import 主通道（engine_adapter）与 CLI 逃生舱（skill_cli）
双通道共用同一份解析代码，从源头杜绝解析结构漂移。

本模块从 ``skill_cli.parse_problem_markdown`` 迁出，行为逐字保持一致。
字段映射严格对应 ProblemMeta / save_problem 期望的扁平 dict。
"""

from __future__ import annotations

import re

from code_tutor_agent.leetcode.leetcode_fetcher import _parse_examples_to_test_cases

# cta-generate-problem 输出契约节（## Title / Topic / Difficulty / Description /
# Examples / Constraints / StarterCode / BruteSolution / OptimalSolution）
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _strip_fences(s: str) -> str:
    """去掉 Markdown 代码围栏（```lang ... ```），无围栏则原样 strip。"""
    m = re.search(r"```[a-zA-Z]*\n?(.*?)```", s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()


def parse_problem_markdown(stdout: str) -> dict | None:
    """从 skill 输出里抠出契约节 → 扁平 dict。

    返回 None 表示解析不到任何契约节（stdout 异常）。
    字段映射严格对应 ProblemMeta（见 schemas/state.py）。
    """
    if not stdout or "##" not in stdout:
        return None
    # 取最后一个 "====" 之后的 body（屏蔽 run 命令自身的前缀日志）
    body = stdout.split("====")[-1] if "====" in stdout else stdout
    parts = _SECTION_RE.split(body)
    # parts: ['', 'Title', '...', 'Topic', '...', ...]
    if len(parts) < 3:
        return None
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip().lower()] = parts[i + 1].strip()

    examples_raw = sections.get("examples", "")
    sample_tcs = _parse_examples_to_test_cases(examples_raw, "")  # 复用现有解析

    fsig = sections.get("functionsignature", "").strip()
    return {
        "title": sections.get("title", "Untitled"),
        "topic": sections.get("topic", "数组"),
        "difficulty": (sections.get("difficulty", "easy") or "easy").lower(),
        "description": sections.get("description", ""),
        # 关键补全：SKILL.md 要求输出 ## FunctionSignature 节（如
        # "grid: List[List[int]] -> int"），但此前解析漏掉了这一节，
        # 导致 function_signature 为空 → 后台 _generate_complex_tests 拿不到
        # 签名 → 随机/边界用例全为 0，判题退化。此处原样提取，
        # 格式与 input_generator.parse_signature 期望的
        # "name: type, ... -> rettype" 完全一致。
        "function_signature": fsig,
        "starter_code": _strip_fences(sections.get("startercode", "")),
        "optimal_solution": _strip_fences(
            sections.get("optimalsolution", "") or sections.get("brutesolution", "")
        ),
        "test_cases": sample_tcs,
    }
