"""POC 测试脚本 — 验证 skill 解析、路由、校验的全流程。

运行方式（项目根目录）：
    uv run python -m src.code_tutor_agent.poc.test_poc

测试内容：
    1. 解析器测试：加载两个 skill，验证 frontmatter/prompt/schema
    2. 路由测试：根据 topic 自动选择对应 skill
    3. Schema 校验测试：输出通过和失败两种情况
    4. 集成测试：mock LLM，验证 invoke 完整流程
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 确保项目 src 在路径中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.code_tutor_agent.poc.skill_parser import parse_skill_md, load_skill
from src.code_tutor_agent.poc.skill_router import SkillRouter


SKILLS_DIR = Path(__file__).resolve().parent / "skills"

passed = 0
failed = 0

def test(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} — {detail}")


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════
#  测试 1：解析器 — 加载两个 skill
# ═══════════════════════════════════════════

section("测试 1：解析器 — 加载 SKILL.md")

try:
    skill_array = load_skill("generate-array", SKILLS_DIR)
    test("generate-array 加载成功", True)
    test("  name 正确", skill_array.name == "generate-array")
    test("  description 不空", bool(skill_array.description))
    test("  model 有值", bool(skill_array.model))
    test("  prompt 模板含占位符", "{topic}" in skill_array.prompt_template and "{difficulty}" in skill_array.prompt_template)
    test("  schema 有必填字段", len(skill_array.schema.get("required", [])) > 0)
    test("  examples 不为空", len(skill_array.examples) > 0)
except Exception as e:
    test(f"generate-array 加载失败: {e}", False)

try:
    skill_dp = load_skill("generate-dp", SKILLS_DIR)
    test("generate-dp 加载成功", True)
    test("  name 正确", skill_dp.name == "generate-dp")
    test("  prompt 含 DP 专项字段", "dp_type" in skill_dp.prompt_template or "DP" in skill_dp.prompt_template)
    test("  schema 含 DP 专用字段", "dp_type" in skill_dp.schema.get("properties", {}))
    test("  schema 含 state_definition", "state_definition" in skill_dp.schema.get("required", []))
except Exception as e:
    test(f"generate-dp 加载失败: {e}", False)

# ═══════════════════════════════════════════
#  测试 2：路由 — 根据 topic 自动选择 skill
# ═══════════════════════════════════════════

section("测试 2：路由 — 根据 topic 选择 skill")

router = SkillRouter(SKILLS_DIR)

# 路由到 DP
skill = router.route("动态规划", "medium")
test("'动态规划' → generate-dp", skill.name == "generate-dp")

skill = router.route("背包问题", "hard")
test("'背包问题' → generate-dp", skill.name == "generate-dp")

# 路由到 Array
skill = router.route("数组", "easy")
test("'数组' → generate-array", skill.name == "generate-array")

skill = router.route("哈希表", "easy")
test("'哈希表' → generate-array", skill.name == "generate-array")

skill = router.route("双指针", "medium")
test("'双指针' → generate-array", skill.name == "generate-array")

# 无匹配 → 默认
skill = router.route("二叉树", "medium")
test("'二叉树'（无匹配）→ 默认 skill", skill.name == "generate-array")

# ═══════════════════════════════════════════
#  测试 3：Schema 校验
# ═══════════════════════════════════════════

section("测试 3：Schema 校验")

# 合法的输出
valid_output = {
    "title": "两数之和",
    "description": "给定一个整数数组 nums 和一个整数 target...",
    "difficulty": "easy",
    "topic": "数组+哈希表",
    "examples": ["Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]"],
    "constraints": ["2 <= nums.length <= 10^4"],
    "function_signature": "nums: List[int], target: int -> List[int]",
    "starter_code": "class Solution:\n    def twoSum(self, nums, target):\n        pass",
    "optimal_solution": "class Solution:\n    def twoSum(self, nums, target):\n        d = {}\n        for i, n in enumerate(nums):\n            if target - n in d:\n                return [d[target - n], i]\n            d[n] = i",
}
ok, errs = skill_array.validate(valid_output)
test("合法输出通过校验", ok, str(errs))

# 缺少必填字段
invalid_output = {"title": "测试", "difficulty": "easy"}
ok, errs = skill_array.validate(invalid_output)
test("缺少必填字段时校验失败", not ok)
test("  报错数正确", len(errs) >= 5, str(errs))

# ═══════════════════════════════════════════
#  测试 4：集成测试 — mock LLM 模拟完整调用
# ═══════════════════════════════════════════

section("测试 4：集成测试 — mock LLM + invoke")

# 模拟 LLM 调用（根据 prompt 内容返回不同 skill 的输出）
def mock_llm(prompt: str) -> str:
    if "动态规划" in prompt:
        return json.dumps({
            "title": "最长递增子序列",
            "description": "给定一个整数数组 nums，找出其中最长的严格递增子序列的长度。",
            "difficulty": "medium",
            "topic": "动态规划",
            "examples": ["Input: nums = [10,9,2,5,3,7,101,18]\nOutput: 4\n解释：最长递增子序列是 [2,3,7,101]，长度为 4。"],
            "constraints": ["1 <= nums.length <= 2500", "-10^4 <= nums[i] <= 10^4"],
            "function_signature": "nums: List[int] -> int",
            "starter_code": "class Solution:\n    def lengthOfLIS(self, nums):\n        pass",
            "optimal_solution": "class Solution:\n    def lengthOfLIS(self, nums):\n        dp = [1] * len(nums)\n        for i in range(len(nums)):\n            for j in range(i):\n                if nums[j] < nums[i]:\n                    dp[i] = max(dp[i], dp[j] + 1)\n        return max(dp)",
            "dp_type": "一维DP",
            "state_definition": "dp[i] 表示以 nums[i] 结尾的最长递增子序列长度",
            "transition_formula": "dp[i] = max(dp[i], dp[j] + 1) 对于所有 j < i 且 nums[j] < nums[i]",
        })
    else:
        return json.dumps({
            "title": "两数之和优化版",
            "description": "给定整数数组 nums 和目标值 target，找出和为目标值的两个数的下标。",
            "difficulty": "easy",
            "topic": "数组+哈希表",
            "examples": ["Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]"],
            "constraints": ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"],
            "function_signature": "nums: List[int], target: int -> List[int]",
            "starter_code": "class Solution:\n    def twoSum(self, nums, target):\n        pass",
            "optimal_solution": "class Solution:\n    def twoSum(self, nums, target):\n        d = {}\n        for i, n in enumerate(nums):\n            if target - n in d:\n                return [d[target - n], i]\n            d[n] = i",
        })

result = skill_array.invoke(mock_llm, topic="数组+哈希表", difficulty="easy")
test("invoke 返回 dict", isinstance(result, dict))
test("  title 正确", result["title"] == "两数之和优化版")
test("  difficulty 正确", result["difficulty"] == "easy")
test("  包含 optimal_solution", "optimal_solution" in result and len(result["optimal_solution"]) > 0)

# mock LLM 返回非法输出
def mock_llm_bad(prompt: str) -> str:
    """模拟 LLM 返回缺少必填字段的输出。"""
    return json.dumps({"title": "不完整"})

try:
    skill_array.invoke(mock_llm_bad, topic="数组", difficulty="easy")
    test("非法输出应抛异常", False, "未抛出异常")
except ValueError as e:
    test("非法输出抛 ValueError", True)
    test("  提示缺少字段", "缺少必填字段" in str(e), str(e)[:100])

# ═══════════════════════════════════════════
#  测试 5：路由器 invoke 一键路由
# ═══════════════════════════════════════════

section("测试 5：路由器 invoke — 一键路由 + 调用")

result = router.invoke("动态规划", "medium", mock_llm)
test("动态规划 → 路由+调用成功", isinstance(result, dict))
test("  title 不空", bool(result.get("title")))

result2 = router.invoke("数组", "easy", mock_llm)
test("数组 → 路由+调用成功", isinstance(result2, dict))
test("  difficulty 正确", result2.get("difficulty") == "easy")


# ═══════════════════════════════════════════
#  总结
# ═══════════════════════════════════════════

section("测试总结")
print(f"  通过: {passed}")
print(f"  失败: {failed}")
print(f"  总计: {passed + failed}")
print(f"\n  {'✅ 全部通过' if failed == 0 else '❌ 有失败项'}")

sys.exit(0 if failed == 0 else 1)