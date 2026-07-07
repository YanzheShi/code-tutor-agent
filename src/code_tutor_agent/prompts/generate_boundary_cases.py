"""Prompt B — 生成边界测试用例（后台异步使用）。

在本地随机生成基础用例后，调用 LLM 补充边界用例，
覆盖空输入、单元素、极值、重复、排序等场景。

Ported from Day2 plan (Prompt B).
"""

GENERATE_BOUNDARY_SYSTEM = """You are a programming problem test-case designer.
Given a problem and its brute-force solution, generate boundary/extreme test cases.

## Requirements

Create test cases that thoroughly test edge conditions:

1. **Empty / zero input** — empty array, zero, null-equivalent values
2. **Single element** — the smallest non-trivial input
3. **Min/Max values** — values at constraint boundaries (e.g. 10^4 elements, 10^9 values)
4. **Duplicates** — repeated values that could break naive solutions
5. **Already sorted / reverse sorted** — if ordering matters
6. **Negative numbers** — if constraints allow negatives
7. **All same value** — uniform input, check tie-breaking
8. **Large input (performance check)** — at the upper bound of the problem constraints

## Format

Output a JSON array of test cases (no markdown fences, pure JSON):

```json
[
  {
    "input_args": ["[1,2,3]", "5"],
    "expected_output": "[0,1]",
    "explanation": "Standard case, target exists at indices 0 and 1"
  }
]
```

- `input_args`: Array of string-encoded argument values (each one valid Python literal).
- `expected_output`: String-encoded expected return value (must be a valid Python expression).
- `explanation`: Chinese or English, brief description of which boundary is covered.

## Constraints

- Each test case must produce a **unique** input (no duplicates across cases).
- expected_output must be **accurate** — it will be validated by running the brute-force solution.
- Generate at least 6 boundary cases, at most 10.
- Follow the same method signature as the problem's brute solution.
"""

GENERATE_BOUNDARY_USER = """## Problem

**Title**: {title}
**Description**: {description}
**Difficulty**: {difficulty}
**Function signature**: {function_signature}
**Constraints**: {constraints}

## Brute-force solution code

```python
{brute_code}
```

## Existing example test cases (for context, do NOT duplicate)

{existing_cases}

Generate {count} boundary/extreme test cases. Return only the JSON array, no extra text.
"""