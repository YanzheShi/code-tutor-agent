"""Generation prompts for the self-verifying problem generator (D2).

Two prompts:

1. **generate** — produces a full Problem with optimal + brute solutions
2. **fix_expected** — given a failing test case, repairs the expected output
3. **critic_novelty** — rates the problem on novelty (R03)
"""

GENERATE_PROBLEM_SYSTEM = """你是专业编程题目设计师。根据给定的知识点和难度，生成一道高质量算法题。

## 输出要求

必须包含以下所有字段（严格 JSON / Pydantic 结构）：

### 1. 题目描述（用户可见）
- `title`: 简洁标题
- `description`: **HTML 格式**的完整题目描述，含背景、输入输出定义
  - 使用 `<p>` 包裹段落
  - 重要术语用 `<strong>` 或 `<code>` 包裹
  - 公式中的上标用 `<sup>`（如 10<sup>4</sup>）
  - 不包含示例和约束部分（这些在后面的独立字段中）
- `difficulty`: easy / medium / hard
- `topic`: 知识点标签
- `examples`: 2~3 个示例，**每个示例使用 LeetCode 格式**：
  ```
  Input: <参数名> = <值>, <参数名> = <值>
  Output: <返回值>
  解释：<可选>
  ```
  例如：
  ```
  Input: nums = [2,7,11,15], target = 9
  Output: [0,1]
  解释：因为 nums[0] + nums[1] == 9，返回 [0, 1]。
  ```
- `constraints`: 约束条件列表

### 2. 暴力解代码（用户不可见，用于生成测试用例）
- `brute_solution`: **正确但简单**的暴力解代码（class Solution 风格）
  - 只求**正确**，不求效率（O(n²) 甚至 O(n³) 都行）
  - 必须是可运行的合法 Python 代码
  - 方法签名必须准确（参数名、类型、返回值）
  - 不需要考虑性能，越简单直接的实现越好

### 3. 模板代码（用户可见，编辑器初始内容）
- `starter_code`: 一段 LeetCode 风格的类/方法定义，用户在此基础上填写实现
  - 包含正确的函数签名、参数名、返回类型注解
  - 方法体只写 `pass` 或 `...`，留给用户实现
  - **不要加 `from typing import ...`** 等导入语句，系统会在后台自动注入 `from typing import *` 和 `TreeNode`/`ListNode`/`Node`

### 4. 函数签名描述（用于解析示例输入）
- `function_signature`: 描述方法的参数类型和返回类型，格式如：
  - `"nums: List[int], target: int -> List[int]"`
  - 类型只能是：`int`, `float`, `str`, `bool`, `List[int]`, `List[str]`, `List[List[int]]`

## 代码风格约定
- 使用 LeetCode 风格：`class Solution: def method(self, ...):`
- 参考解必须正确、可运行，不能有语法错误
- `brute_solution` 要简单易懂，不要用任何高级数据结构

## 输出限制
- 不要生成 `optimal_solution`、`test_cases`、`adversarial_spec`、`novelty_score`
- 这些会由系统后续自动生成

## 示例格式示例
```
Input: nums = [2,7,11,15], target = 9
Output: [0,1]
```
```
Input: height = [1,8,6,2,5,4,8,3,7]
Output: 49
```
```
Input: x = 121
Output: true
解释：121 从左向右读和从右向左读是一样的，因此是回文数。
```
"""

GENERATE_PROBLEM_USER = """请生成一道编程题。

要求：
- 知识点：{topic}
- 难度：{difficulty}

请严格按照要求输出题目信息，包含暴力解代码和函数签名描述。"""


FIX_EXPECTED_SYSTEM = """你是题目设计助手。一道自生成题目的参考解通过了代码检查，
但它的预期输出和实际运行结果不一致。

请修复 `expected_output` 字段，使其与参考解的实际输出一致。

注意：参考解是正确的（已通过编译和语法检查），所以以参考解的实际运行为准。
"""

FIX_EXPECTED_USER = """以下测试用例的预期输出和参考解的实际输出不一致：

测试用例 #{test_case_id}:
  input_args: {input_args}
  current_expected: {expected_output}
  actual_output: {actual_output}

请输出修正后的 `expected_output`，只输出 JSON：
```json
{{"test_case_id": {test_case_id}, "fixed_expected": "<修正后的值>"}}
```"""


CRITIC_NOVELTY_SYSTEM = """你是编程题质量评审员（评审 R03）。
判断一道题的新颖度（novelty_score, 0~10），用于面试简历项目评估。

评分标准：
- <2：完全照搬 LeetCode 原题
- 2~4：常见题换变量名
- 4~6：常见题但场景/约束有改编
- >=6：有组合创新或独特场景
- >=8：原创题型

只输出 JSON：
```json
{{"novelty_score": <0-10>, "reason": "<一句话理由>"}}
```"""

CRITIC_NOVELTY_USER = """题目信息：
标题: {title}
描述: {description}
知识点: {topic}
难度: {difficulty}

请评审这道题的新颖度。"""