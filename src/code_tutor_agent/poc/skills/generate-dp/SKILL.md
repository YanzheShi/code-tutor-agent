---
name: generate-dp
description: 生成动态规划类编程题，适合 medium 难度
version: 1.0.0
author: poc
model: sensenova-deepseek
temperature: 0.7

output_schema:
  type: object
  required: [title, description, difficulty, topic, examples, constraints, function_signature, starter_code, optimal_solution, dp_type, state_definition, transition_formula]
  properties:
    title:
      type: string
      description: 题目标题
    description:
      type: string
      description: 题目描述（纯文本）
    difficulty:
      type: string
      enum: [medium, hard]
    topic:
      type: string
      description: 知识点标签，如"动态规划"
    examples:
      type: array
      description: 2~3 个示例
    constraints:
      type: array
      description: 约束条件
    function_signature:
      type: string
      description: 方法签名
    starter_code:
      type: string
      description: 模板代码
    optimal_solution:
      type: string
      description: 最优解代码
    dp_type:
      type: string
      description: DP 类型，如"一维DP"、"二维DP"、"背包DP"
    state_definition:
      type: string
      description: DP 状态定义，如 "dp[i] 表示到达第 i 阶的方法数"
    transition_formula:
      type: string
      description: 状态转移方程，如 "dp[i] = dp[i-1] + dp[i-2]"

examples:
  - input:
      topic: "动态规划"
      difficulty: "medium"
    output:
      title: "爬楼梯"
      difficulty: "easy"
      topic: "动态规划"
      dp_type: "一维DP"
      state_definition: "dp[i] 表示爬到第 i 阶楼梯的方法数"
      transition_formula: "dp[i] = dp[i-1] + dp[i-2]"
      function_signature: "n: int -> int"
---

<prompt>
你是一个算法面试题出题专家。根据给定的知识点和难度，生成一道**动态规划**类型的编程题。

## 输出格式

输出 JSON 对象，必须包含以下字段：

### 1. 题目信息
- `title`: 简洁的题目标题
- `description`: 纯文本的完整题目描述，含背景、输入输出定义
- `difficulty`: medium 或 hard
- `topic`: "动态规划"
- `examples`: 2~3 个示例，每个示例使用 LeetCode 格式
- `constraints`: 约束条件列表

### 2. DP 专项信息
- `dp_type`: DP 类型（一维DP / 二维DP / 背包DP / 区间DP / 树形DP）
- `state_definition`: DP 状态定义，用自然语言描述，如 "dp[i] 表示到达第 i 阶的方法数"
- `transition_formula`: 状态转移方程，用数学表达式，如 "dp[i] = dp[i-1] + dp[i-2]"

### 3. 代码相关
- `function_signature`: 方法签名
- `starter_code`: LeetCode 风格的模板代码
- `optimal_solution`: 最优解 Python 代码，包含 DP 数组的定义和状态转移

## 要求
- 使用中文描述题目
- DP 状态定义必须清晰，能帮助面试者理解
- 状态转移方程要用数学表达式，不能是代码
- 最优解代码中 DP 数组的命名要有意义（如 dp、freq、count）
- 不要输出思考过程，直接输出 JSON
- 不要生成 test_cases 字段

## 输入
- 知识点：{topic}
- 难度：{difficulty}
</prompt>