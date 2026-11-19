"""静态闸单元测试（guards/code_quality.py，2026-11-19）。

覆盖目标：
- 截断型题解（无 return）必须拒绝；
- 「注释填充」只有在可执行代码也很少时才拒绝（不误伤正常带注释的题解）；
- CoT 扫描覆盖 examples 与英文注释；
- 主类不是 Solution / 语法错不在这里报（各自有专门闸门）。
"""

from __future__ import annotations

from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.generation.verifier import CodeVerifier
from code_tutor_agent.guards.code_quality import (
    code_cot_issues,
    code_quality_issues,
    example_issues,
    strip_cot_comments,
    strip_cot_comments_in_dict,
    text_cot_issues,
)

_OK_SOLUTION = (
    "class Solution:\n"
    "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
    "        seen = {}\n"
    "        for i, n in enumerate(nums):\n"
    "            if target - n in seen:\n"
    "                return [seen[target - n], i]\n"
    "            seen[n] = i\n"
    "        return []\n"
)


# ── code_quality_issues ──────────────────────────────────────────


def test_normal_solution_passes():
    assert code_quality_issues(_OK_SOLUTION, field="optimal_solution") == []


def test_empty_code_is_skipped():
    assert code_quality_issues("", field="optimal_solution") == []
    assert code_quality_issues("   \n", field="optimal_solution") == []


def test_no_return_rejected():
    code = (
        "class Solution:\n"
        "    def solve(self, nums: list[int]) -> int:\n"
        "        total = 0\n"
        "        for n in nums:\n"
        "            total += n\n"
    )
    issues = code_quality_issues(code, field="optimal_solution")
    assert any("没有任何 return" in i for i in issues)


def test_return_inside_nested_function_does_not_count():
    code = (
        "class Solution:\n"
        "    def solve(self, x: int) -> int:\n"
        "        def helper(v):\n"
        "            return v + 1\n"
        "        helper(x)\n"
    )
    issues = code_quality_issues(code, field="optimal_solution")
    assert any("没有任何 return" in i for i in issues)


def test_comment_heavy_but_enough_code_passes():
    code = (
        "# 说明：这题用哈希表做，时间 O(n)\n" * 12
        + "class Solution:\n"
        + "    def solve(self, nums: list[int]) -> int:\n"
        + "        seen = {}\n" * 10
        + "        return len(seen)\n"
    )
    assert code_quality_issues(code, field="optimal_solution") == []


def test_comment_filler_rejected():
    code = (
        "# 分析：先考虑暴力解，再考虑优化……\n" * 20
        + "class Solution:\n"
        + "    def solve(self, nums: list[int]) -> int:\n"
        + "        total = 0\n"
        + "        for n in nums:\n"
        + "            total += n\n"
    )
    issues = code_quality_issues(code, field="optimal_solution")
    joined = " | ".join(issues)
    assert "注释占比" in joined
    assert "没有任何 return" in joined


def test_class_solution_without_method_reported():
    issues = code_quality_issues("class Solution:\n    pass\n", field="optimal_solution")
    assert any("没有任何方法" in i for i in issues)


def test_design_class_is_not_flagged_here():
    code = (
        "class MinStack:\n"
        "    def __init__(self):\n"
        "        self.st = []\n"
    )
    assert code_quality_issues(code, field="optimal_solution") == []


def test_syntax_error_is_not_reported_here():
    assert code_quality_issues("def broken(:", field="optimal_solution") == []


# ── CoT ──────────────────────────────────────────────────────────


def test_cot_in_english_comment_rejected():
    code = "# Let's think about the prefix sums\n" + _OK_SOLUTION
    issues = code_cot_issues(code, field="optimal_solution")
    assert issues and "注释含思维链痕迹" in issues[0]


def test_cot_in_chinese_comment_rejected():
    code = "# 等一下，让我重新推导\n" + _OK_SOLUTION
    issues = code_cot_issues(code, field="optimal_solution")
    assert issues and "注释含思维链痕迹" in issues[0]


def test_normal_comment_passes():
    code = "# 用哈希表记录已访问元素\n" + _OK_SOLUTION
    assert code_cot_issues(code, field="optimal_solution") == []


def test_cot_in_example_rejected():
    example = (
        "Input: nums = [1, 2], target = 3\n"
        "Output: 1\n"
        "解释：等一下，让我重新推导一下这个例子。"
    )
    issues = example_issues(example, where="examples[0]")
    assert any("思维链痕迹" in i for i in issues)


def test_long_explanation_rejected():
    example = (
        "Input: nums = [1, 2], target = 3\n"
        "Output: 1\n"
        "解释：" + "先看第一个元素再看第二个元素，然后比较大小，" * 30
    )
    issues = example_issues(example, where="examples[0]")
    assert any("解释过长" in i for i in issues)


def test_normal_example_passes():
    example = (
        "Input: nums = [2,7,11,15], target = 9\n"
        "Output: [0,1]\n"
        "解释：nums[0] + nums[1] == 9，因此返回 [0,1]。"
    )
    assert example_issues(example, where="examples[0]") == []


def test_description_cot_still_works():
    issues = text_cot_issues("让我们一步一步来分析", where="description")
    assert issues and "思维链痕迹" in issues[0]


# ── 确定性修复：剥 CoT 注释 ──────────────────────────────────────


def test_strip_cot_comments_removes_only_whole_line_cot():
    code = (
        "# 用哈希表记录已访问元素\n"
        + _OK_SOLUTION
        + "# Wait, this formula is wrong\n"
        + "# 正常说明：时间复杂度 O(n)\n"
    )
    cleaned, removed = strip_cot_comments(code)
    assert removed == 1
    assert "Wait" not in cleaned
    assert "用哈希表" in cleaned
    assert "正常说明" in cleaned


def test_strip_cot_comments_keeps_inline_directives():
    code = "# type: ignore\n" + _OK_SOLUTION
    cleaned, removed = strip_cot_comments(code)
    assert removed == 0
    assert cleaned == code


def test_strip_cot_comments_does_not_fix_truncated_solution():
    truncated = (
        "class Solution:\n"
        "    def solve(self, nums: list[int]) -> int:\n"
        "        total = 0\n"
        "        # Wait, I need to think again\n"
    )
    cleaned, removed = strip_cot_comments(truncated)
    assert removed == 1
    # 剥完仍然没有 return，静态闸依旧拒绝——不会被误救
    assert any("没有任何 return" in i for i in code_quality_issues(cleaned, field="optimal_solution"))


def test_strip_cot_comments_in_dict_handles_both_fields():
    d = {
        "optimal_solution": "# Let's think\n" + _OK_SOLUTION,
        "brute_solution": "# 等一下，让我重新推导\n" + _OK_SOLUTION,
        "description": "让我们开始吧",
    }
    changed, removed = strip_cot_comments_in_dict(d)
    assert changed and removed == 2
    assert "Let's think" not in d["optimal_solution"]
    assert "等一下" not in d["brute_solution"]
    assert d["description"] == "让我们开始吧"  # 只碰代码字段


# ── CodeVerifier 集成 ───────────────────────────────────────────


def _draft(**overrides) -> ProblemDraft:
    kwargs = dict(
        topic="数组",
        difficulty="easy",
        title="Two Sum",
        description="Given an array of integers nums and an integer target, return indices.",
        starter_code=(
            "class Solution:\n"
            "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
            "        pass\n"
        ),
        optimal_solution=_OK_SOLUTION,
        function_signature="nums: list[int], target: int -> list[int]",
        examples=["Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]"],
    )
    kwargs.update(overrides)
    return ProblemDraft(**kwargs)


def test_code_verifier_rejects_truncated_solution():
    truncated = (
        "class Solution:\n"
        "    def solve(self, nums: list[int]) -> int:\n"
        "        total = 0\n"
        "        # 然后这里还没想好\n"
    )
    ok, issues = CodeVerifier().verify(_draft(optimal_solution=truncated))
    assert not ok
    assert any("没有任何 return" in i for i in issues)


def test_code_verifier_accepts_normal_draft():
    ok, issues = CodeVerifier().verify(_draft())
    assert ok, issues


def test_code_verifier_rejects_cot_in_example():
    ok, issues = CodeVerifier().verify(
        _draft(examples=["Input: nums = [1]\nOutput: 1\n解释：等一下，我算错了。"])
    )
    assert not ok
    assert any("examples[0]" in i and "思维链痕迹" in i for i in issues)
