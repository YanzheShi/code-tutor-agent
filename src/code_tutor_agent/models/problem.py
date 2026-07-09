"""Problem model for code tutoring — lightweight version (Day2).

LLM now only generates:
- title / description / difficulty / topic / examples / constraints
- optimal_solution (for test generation and AC display)
- starter_code (LeetCode template)
- function_signature (for random input generator)

Test cases, brute_solution, adversarial spec are generated locally later.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Problem(BaseModel):
    """编程题目 —— Day2 轻量版：LLM 只出描述+暴力解，测试用例本地生成。"""

    # ── 用户可见 ──
    title: str = Field(description="题目标题")
    description: str = Field(description="题目描述，含背景、输入输出定义、示例")
    difficulty: str = Field(description="easy / medium / hard")
    topic: str = Field(description="知识点，如 数组、双指针、动态规划")
    examples: List[str] = Field(description="示例列表，每个元素是一个完整示例")
    constraints: List[str] = Field(description="约束条件列表")

    # ── 暴力解（本地生成测试用例用）──
    brute_solution: str = Field(
        default="",
        description="暴力解代码（class Solution 风格），仅用于跑测试用例生成预期输出",
    )

    # ── 模板代码 ──
    starter_code: str = Field(
        default="",
        description="LeetCode 风格模板代码，如 class Solution: def solve(...): pass",
    )

    # ── 函数签名描述（本地随机输入生成器用）──
    function_signature: str = Field(
        default="",
        description="参数类型描述，如 'nums: List[int], target: int -> List[int]'",
    )

    # ── 以下字段不再由 LLM 生成，但保留模型兼容性 ──
    # （系统本地生成后用 update_problem 填入）
    test_cases: List = Field(
        default_factory=list,
        description="完整测试用例（系统本地生成，非 LLM 输出）",
    )
    optimal_solution: str = Field(
        default="",
        description="最优解代码（AC 后显示给用户）",
    )
    alternative_solutions: list[str] = Field(
        default_factory=list,
        description="备选解法（仅显示用，不参与判题）",
    )
    novelty_score: float = Field(
        default=7.0, ge=0.0, le=10.0,
        description="新颖度评分",
    )