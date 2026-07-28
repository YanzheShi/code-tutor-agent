---
name: generate-array
description: 生成数组/哈希表/双指针类编程题，适合 easy~medium 难度
version: 1.0.0
author: poc
model: sensenova-deepseek
temperature: 0.7

output_schema:
  type: object
  required: [title, description, difficulty, topic, examples, constraints, function_signature, starter_code, optimal_solution]
  properties:
    title:
      type: string
      description: 题目标题
    description:
      type: string
      description: 题目描述（纯文本）
    difficulty:
      type: string
      enum: [easy, medium]
    topic:
      type: string
      description: 知识点标签
    examples:
      type: array
      description: 2~3 个示例，每个示例用 LeetCode 格式
    constraints:
      type: array
      description: 约束条件列表
    function_signature:
      type: string
      description: '方法签名，如 "nums: List[int], target: int -> List[int]"'
    starter_code:
      type: string
      description: 模板代码，class Solution 风格
    optimal_solution:
      type: string
      description: 最优解代码

examples:
  - input:
      topic: "数组+哈希表"
      difficulty: "easy"
    output:
      title: "两数之和"
      difficulty: "easy"
      topic: "数组+哈希表"
      function_signature: "nums: List[int], target: int -> List[int]"
---

<prompt>
你是一个算法面试题出题专家。根据给定的知识点和难度，生成一道**数组/哈希表/双指针**类型的编程题。

## 输出格式

输出 JSON 对象，必须包含以下字段：

### 1. 题目信息
- `title`: 简洁的题目标题
- `description`: 纯文本的完整题目描述，含背景、输入输出定义
- `difficulty`: easy 或 medium
- `topic`: 知识点标签（如"数组"、"数组+哈希表"、"双指针"）
- `examples`: 2~3 个示例，每个示例使用 LeetCode 格式：
  ```
  Input: nums = [2,7,11,15], target = 9
  Output: [0,1]
  解释：因为 nums[0] + nums[1] == 9，返回 [0, 1]。
  ```
- `constraints`: 约束条件列表，如 ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"]

### 2. 代码相关
- `function_signature`: 方法签名，描述参数类型和返回类型，如 "nums: List[int], target: int -> List[int]"
- `starter_code`: LeetCode 风格的模板代码，方法体只写 pass
- `optimal_solution`: 正确且高效的最优解 Python 代码，class Solution 风格

## 要求
- 使用中文描述题目
- 最优解必须使用该知识点最典型的算法
- 不要输出思考过程，直接输出 JSON
- 不要生成 test_cases 字段

## 输入
- 知识点：{topic}
- 难度：{difficulty}
</prompt>