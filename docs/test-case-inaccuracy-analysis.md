# 测试用例不准确 — 分析与修复方案

## 问题概述

LLM 出题后生成的测试用例存在 expected_output 不正确的问题，导致用户正确代码被判 WA。

## 全量管线图

```
┌─────────────────────────────────────────────────┐
│ 生成阶段 (generator.py)                          │
│                                                 │
│  LLM 出题 → Problem {                           │
│    title, description, examples, constraints,    │
│    optimal_solution (参考解),                    │
│    starter_code, function_signature              │
│  }                                              │
│       ↓                                         │
│  _parse_examples_to_test_cases(examples)         │
│    → 2 个可见示例用例                            │
│       ↓                                         │
│  run_solution(参考解, 示例用例, local)            │
│    → 回填 expected_output                       │
│       ↓                                         │
│  save_problem() → DB (仅存 2 个可见用例)          │
└─────────────────────────────────────────────────┘
       ↓  (后台，用户已开始做题)
┌─────────────────────────────────────────────────┐
│ 后台生成 (generation.py)                         │
│  _generate_complex_tests()                       │
│                                                 │
│  generate_random_inputs(func_sig, count=12)      │
│    → 12 组随机输入                               │
│       ↓                                         │
│  run_solution(参考解, 随机输入, local)            │
│    → 回填 expected_output                        │
│       ↓                                         │
│  LLM 生成边界用例 (8 个)                         │
│       ↓                                         │
│  run_solution(参考解, 边界用例, local)            │
│    → 回填 expected_output                        │
│       ↓                                         │
│  重验证示例用例 → run_solution(参考解, local)      │
│       ↓                                         │
│  update_problem_test_cases() → DB 完整用例        │
└─────────────────────────────────────────────────┘
       ↓
┌─────────────────────────────────────────────────┐
│ 判题阶段 (judge.py)                              │
│  run_solution(用户代码, 测试用例, judge0?)        │
│    → 比较 expected_output vs actual              │
│    → 全对 = AC, 否则 = WA                       │
└─────────────────────────────────────────────────┘
```

## 故障点分析

### 故障点 A: 参考解没有独立验证 ⚠️ 最严重

**位置：** `generator.py:375` → `generate_problem()`，`generator.py:407-424`

**现象：** 整个管线依赖的 `optimal_solution` 是 LLM 生成的，从未独立验证。
如果 LLM 生成有 bug 的参考解，所有测试用例的 expected_output 都会错。

**代码：**
```python
# generator.py:383
brute_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "")
# 只有 optimal_solution 有值，brute_solution 为空
```

### 故障点 B: 随机输入范围完全忽略 constraints

**位置：** `input_generator.py:49-53`

**现象：** 硬编码 `min_val=-100, max_val=100`，完全不看题目 constraints。
`constraints` 参数只用于检测"有序"关键词，不参与取值范围。

**代码：**
```python
def _random_list_int(min_len=2, max_len=8, min_val=-100, max_val=100) -> str:
```

### 故障点 C: LLM 提供的 expected_output 被信任

**位置：** `generator.py:407-411`

**现象：** LLM 在 examples 中自带的 expected_output 被直接信任，跳过验证。

**代码：**
```python
expected = tc.get("expected_output", "")
if expected and expected not in ("", "..."):
    continue  # ← 跳过验证！LLM 给的 expected 直接当正确值
```

### 故障点 D: 边界用例验证依赖同一参考解

**位置：** `generation.py:340-359`

**现象：** LLM 边界用例的 expected 用 `run_solution(参考解, ...)` 回填，
如果参考解本身有 bug，验证毫无意义。

### 故障点 E: 生成流程可能走 Judge0

**位置：** `runner.py:117-143`

**现象：** 如果配置了 `JUDGE0_URL`，`run_solution` 会优先走 Judge0。
生成流程跑的是参考解（可信代码），不需要沙箱隔离，Judge0 慢且无必要。

---

## 修复方案

### P0-1: 参考解自验证

**目标：** 拦截参考解连 example 都过不了的明显 bug。

**方案：** 在 `generator_node` 中，对 LLM 生成的 `optimal_solution` 做自验证：
用示例用例跑参考解，检查是否全部 Passed。参考解连 example 都过不了 → 重试。

**修改文件：** `nodes/generator.py`

**代码：**
```python
# 在 generator.py 的 generator_node 中，保存之前加：
for tc in sample_tcs:
    results = run_solution(brute_code, [tc], timeout=10.0, function_signature=func_sig)
    if not results or results[0].status != "Passed":
        logger.warning("optimal_solution failed on example — retrying")
        all_ok = False
        break
```

**风险：** 无。合法参考解必然过 example，这只拦住明显错误。

### P0-2: 生成流程强制走本地 subprocess

**目标：** 生成测试用例时永远不走 Judge0，避免 Judge0 慢速影响。

**方案：** 给 `run_solution` 加 `force_local=True` 参数，生成流程调用时传此参数，
跳过 Judge0 尝试，直接走本地 subprocess。

**修改文件：** `sandbox/runner.py`，`api/services/generation.py`，`nodes/generator.py`

**代码：**
```python
# runner.py: 加 force_local 参数
def run_solution(code, test_cases, timeout=..., function_signature=None, force_local=False):
    if not force_local:
        judge0_url = os.getenv("JUDGE0_URL")
        if judge0_url:
            ...  # 尝试 Judge0
    # 直接走本地 subprocess
```

**风险：** 无。生成流程跑的代码全是参考解（可信代码），不需要沙箱。

### P0-3: LLM 同时生成 optimal_solution 和 brute_solution

**目标：** 为将来的双参考解交叉验证（P3）做准备。

**方案：** 
1. 修改 `prompts/generate_problem.py`，要求 LLM 同时生成 `optimal_solution`（最优解）和 `brute_solution`（暴力解，思路简单但可能慢）
2. 在 `verify_problem()` 中验证 `brute_solution` 也能编译通过
3. 在 `generator.py` 中将 `brute_solution` 也保存到 DB
4. 在 `generation.py` 的 `_generate_complex_tests()` 中优先使用 `optimal_solution` 作为参考解，保留 `brute_solution` 作为备选

**修改文件：** `prompts/generate_problem.py`，`agents/agent_problem.py`，`nodes/generator.py`

**风险：** LLM 输出 token 会增加，但 `max_tokens=8192` 有余量。

### P1: 不信任 LLM 的 expected

**目标：** 修复 LLM 编错 expected 的问题。

**方案：** 修改 `generator.py:407-411`，不再跳过 LLM 提供的 expected，一律用参考解重新计算并覆盖。

**修改文件：** `nodes/generator.py`

**代码：**
```python
# 去掉 skip，一律用参考解重新计算
for tc in sample_tcs:
    results = run_solution(brute_code, [tc], timeout=10.0, function_signature=func_sig)
    if results and results[0].status == "Passed":
        tc["expected_output"] = results[0].detail
    else:
        all_ok = False
```

**风险：** 如果参考解有 bug 且 example 也过不了，会导致重试。这正是 P0-1 想拦截的。

### P2: 随机输入尊重 constraints

**目标：** 让随机输入更贴近题目范围。

**方案：** 在 `input_generator.py` 中解析 `constraints` 参数提取数值范围。

**需要解析的格式：**
```
"0 <= nums.length <= 10^4"
"-10^9 <= nums[i] <= 10^9"
"0 <= Node.val <= 100"
```

**修改文件：** `sandbox/input_generator.py`

**关键代码：**
```python
import re

# 从 constraints 中提取数值范围
_LOWER_BOUND_RE = re.compile(r"(?:大于等于|>=|≥)\s*(-?\d+(?:\^\d+)?)")
_UPPER_BOUND_RE = re.compile(r"(?:小于等于|<=|≤)\s*(-?\d+(?:\^\d+)?)")
_INT_RE = re.compile(r"-?\d+")

def _parse_ranges(constraints, description):
    """从 constraints 中提取数值范围，用于生成随机输入"""
    min_val, max_val = -100, 100  # 默认值
    for text in constraints or []:
        # 检测 nums[i] 或 Node.val 的范围
        lows = _LOWER_BOUND_RE.findall(text)
        highs = _UPPER_BOUND_RE.findall(text)
        for v in lows:
            val = _eval_power(v)
            if val < min_val: min_val = val
        for v in highs:
            val = _eval_power(v)
            if val > max_val: max_val = val
    return min_val, max_val

def _eval_power(s: str) -> int:
    """处理 10^4 这样的写法"""
    if '^' in s:
        base, exp = s.split('^')
        return int(base) ** int(exp)
    return int(s)
```

**风险：** 低。解析失败时 fallback 到默认值，不影响功能。

### P3: 双参考解交叉验证

**目标：** 最可靠的验证方案。

**方案：** `_generate_complex_tests()` 中，对每个测试用例，用 `optimal_solution` 和 `brute_solution` 分别跑，比较输出是否一致。不一致的用例丢弃并记录日志。

**修改文件：** `api/services/generation.py`

**关键代码：**
```python
def _generate_complex_tests(problem_id, sid):
    ...
    optimal_code = full.optimal_solution
    brute_code = full.brute_solution
    
    # 对每个随机输入
    for idx, inp in enumerate(random_inputs):
        ...
        # 用 optimal_solution 跑
        opt_results = run_solution(optimal_code, [tc], ..., force_local=True)
        # 用 brute_solution 跑
        brute_results = run_solution(brute_code, [tc], ..., force_local=True)
        
        if opt_results and brute_results:
            opt_actual = opt_results[0].detail or ""
            brute_actual = brute_results[0].detail or ""
            if opt_actual == brute_actual:
                tc["expected_output"] = opt_actual
                all_tcs.append(tc)
            else:
                logger.warning(f"Cross-validation mismatch: opt={opt_actual} brute={brute_actual}, dropping")
        else:
            logger.warning("One or both reference solutions failed, dropping")
```

**风险：** 中等。需要 `optimal_solution` 和 `brute_solution` 都在，且两者输出一致才接受用例。
如果两个解都对同一输入输出不同（比如排序题，`brute` 用 `sorted` 而 `optimal` 用 `list.sort`，
但理论上两者输出应一致），则用例被丢弃。这会导致测试用例数量减少，但留下的都是可信的。

---

## 实施顺序

```
P0-1: 参考解自验证      → generator.py (~10行)
P0-2: 强制本地 subprocess → runner.py, generation.py, generator.py (~15行)
P0-3: 同时生成双解       → generate_problem.py, agent_problem.py, generator.py (~20行)
  ↓
P1: 不信任 LLM expected → generator.py (~5行)
  ↓
P2: 随机输入尊重约束     → input_generator.py (~40行)
  ↓
P3: 双参考解交叉验证     → generation.py (~60行)
```

## 依赖关系

- P0-1 和 P0-2 无依赖，可并行
- P0-3 是 P3 的前置条件
- P1 依赖 P0-1（确保参考解可信后再覆盖 expected）
- P2 无依赖，可独立实施
- P3 依赖 P0-3（需要两个参考解都存在）