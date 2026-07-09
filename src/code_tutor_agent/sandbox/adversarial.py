"""对抗策略引擎 — Adversarial Strategy Engine

**设计决策**（面试考点）：

为什么需要对抗测试？
    传统 OJ 的「预设用例」只能验证代码是否"对给定输入正确"。
    对抗测试要回答：「你的代码在我不给你好果子吃的时候，还能扛住吗？」
    — 规模对抗测性能（TLE），边界对抗测鲁棒性（WA/RE）。

为什么混合策略（LLM + 规则）？
    PRD §8.3 有详细 trade-off 分析：
    - 边界对抗 → 纯规则：边界是确定性的，规则 100% 覆盖，不必 LLM
    - 规模对抗 → LLM 出"分布特征" + 规则拼数组：纯规则拼的数组太假（全 1 数组测不出真 TLE），
      纯 LLM 拼又贵又不定型。折中：LLM 只描述特征，规则生成数据。

三个阶段为什么严格串行？
    基础判题 → 对抗 → 评审。因为：
    1. 基础挂了 → 不跑对抗（节约 token + 时间）
    2. 基础过了但对抗挂了 → 交辅导（用户代码有隐藏问题）
    3. 都过了 → 才跑评审（复杂度+风格）
    4. 评审不拦 AC — AC 就是 AC，评审只影响「下一题难度」和「用户画像」
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from code_tutor_agent.config import get_llm
from code_tutor_agent.sandbox.runner import (
    RunnerResult,
    run_solution,
)

logger = logging.getLogger(__name__)

# ── 可配置参数 ──
SCALE_ADV_TIMEOUT = 3.0       # 规模对抗单次超时（秒），O(n²) 暴力解在这个时间内应 TLE
SCALE_MIN_N = 5_000           # 规模对抗最小元素数
BOUNDARY_TIMEOUT = 2.0        # 边界对抗单次超时

# ──────────────────────────────────────────────
#  第一步：代码弱点分析（AST 启发式，零 LLM 调用）
# ──────────────────────────────────────────────


def analyze_code_weakness(code: str) -> dict[str, Any]:
    logger.info("▶ analyze_code_weakness()")
    """分析用户代码，识别可能的弱点类型（纯 AST 分析，无 LLM 调用）。

    返回一个 dict，包含：
    - ``weakness_type``: str — 弱点分类标签
    - ``confidence``: float — 0~1，置信度
    - ``detail``: str — 人类可读的描述
    - ``suggested_attack``: str — 建议的对抗策略

    面试考点：为什么这里不用 LLM？
        — 弱点分类是模式匹配问题，AST 启发式足够快且零成本。
        — LLM 调用留给「生成对抗用例特征描述」那一步（需要语义理解）。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "weakness_type": "syntax_error",
            "confidence": 1.0,
            "detail": "代码有语法错误，无法分析",
            "suggested_attack": "none",
        }

    # ── 检查特征 ──
    has_nested_loop = _has_nested_loop(tree)
    has_recursion = _has_recursion(tree)
    has_memo = _has_memo(tree)
    has_only_null_check = _has_only_null_check(tree)
    has_dp_pattern = _has_dp_pattern(tree)
    loop_count = _count_loops(tree)

    # ── 决策树 ──
    if has_recursion and not has_memo:
        return {
            "weakness_type": "recursion_no_memo",
            "confidence": 0.85,
            "detail": "使用了递归但没有记忆化（Memoization），可能导致指数级重复计算",
            "suggested_attack": "deep_recursion",
        }

    if has_nested_loop and loop_count >= 2:
        return {
            "weakness_type": "brute_force_n2",
            "confidence": 0.75,
            "detail": f"检测到 {loop_count} 层嵌套循环，时间复杂度可能是 O(n^{loop_count})",
            "suggested_attack": "large_scale",
        }

    if has_only_null_check:
        return {
            "weakness_type": "boundary_only_null",
            "confidence": 0.7,
            "detail": "只检查了空输入，未覆盖其他边界（单元素、重复、极值）",
            "suggested_attack": "edge_boundary",
        }

    if has_dp_pattern:
        return {
            "weakness_type": "dp_no_optimization",
            "confidence": 0.5,
            "detail": "检测到动态规划模式，但未验证空间优化（滚动数组等）",
            "suggested_attack": "large_scale",
        }

    return {
        "weakness_type": "unknown",
        "confidence": 0.3,
        "detail": "未识别出明显弱点模式，运行通用对抗",
        "suggested_attack": "comprehensive",
    }


def _has_nested_loop(tree: ast.AST) -> bool:
    """检测是否有嵌套循环（for/while 内部包含另一个 for/while）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if child is node:
                    continue
                if isinstance(child, (ast.For, ast.While)):
                    return True
    return False


def _count_loops(tree: ast.AST) -> int:
    """统计最深嵌套循环深度。"""
    max_depth = 0

    def _walk(node, depth):
        nonlocal max_depth
        if isinstance(node, (ast.For, ast.While)):
            depth += 1
            max_depth = max(max_depth, depth)
        for child in ast.iter_child_nodes(node):
            _walk(child, depth)

    _walk(tree, 0)
    return max_depth


def _has_recursion(tree: ast.AST) -> bool:
    """检测函数内部是否调用了自身（递归）。

    支持两种模式：
        - ``fib(n-1)`` — 直接调用（ast.Name）
        - ``self.fib(n-1)`` — 方法调用（ast.Attribute）
    """
    func_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # 直接调用: fib(...)
            if isinstance(node.func, ast.Name) and node.func.id in func_names:
                return True
            # 方法调用: self.fib(...)
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in func_names
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                return True
    return False


def _has_memo(tree: ast.AST) -> bool:
    """检测是否使用了记忆化（@cache / lru_cache / dict memo 模式）。"""
    source = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    if not source:
        return False
    # 检查装饰器
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_name = (
                    dec.attr if isinstance(dec, ast.Attribute)
                    else dec.id if isinstance(dec, ast.Name)
                    else ""
                )
                if dec_name in ("cache", "lru_cache", "memo"):
                    return True
    # 检查 dict-based memo
    return bool(re.search(r"\bmemo\b|\bcache\b|\bseen\b", source))


def _has_only_null_check(tree: ast.AST) -> bool:
    """检测是否只检查了空输入（缺少其他边界处理）。"""
    checks = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            source = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "None" in source or "not" in source:
                checks.add("null")
            if "len" in source or "== 0" in source or "== []" in source:
                checks.add("empty")
    return len(checks) <= 1 and "empty" in checks


def _has_dp_pattern(tree: ast.AST) -> bool:
    """检测动态规划模式（dp 数组/表）。"""
    source = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    return bool(re.search(r"\bdp\b|\btable\b|\bgrid\b", source))


# ──────────────────────────────────────────────
#  第二步：边界对抗 — 纯规则生成（零 LLM 调用）
# ──────────────────────────────────────────────


def generate_boundary_cases(problem_dict: dict) -> list[dict]:
    logger.info("▶ generate_boundary_cases()")
    """根据题目约束，枚举边界测试用例（纯规则）。

    设计原则（PRD §8.3）：
        「边界是确定性的，规则 100% 覆盖，不必 LLM」

    生成策略：
        1. 从 constraints 解析 n 的下界 → 空数组 / 单元素 / 双元素
        2. 从 constraints 解析值的范围 → min / max / 负数 / 零 / 重复
        3. 组合上述维度生成 ~5-8 个用例

    面试考点：为什么这个函数不用 LLM？
        — 边界情况是确定性的组合问题。给 LLM 做既贵又容易漏。
        — 规则的确定性保证：你永远不会漏掉「空数组」这个用例。
    """
    constraints = problem_dict.get("constraints", [])
    test_cases = problem_dict.get("test_cases", [])
    base_inputs = _parse_constraints(constraints, test_cases)

    cases: list[dict] = []

    # ── 空数据 ──
    if base_inputs.get("min_n", 1) == 0:
        cases.append({
            "input_args": _adapt_args(base_inputs, []),
            "expected_output": "",
            "explanation": "空数组输入边界",
        })

    # ── 单元素 ──
    if base_inputs.get("min_n", 1) <= 1:
        cases.append({
            "input_args": _adapt_args(base_inputs, base_inputs.get("single_val", [0])),
            "expected_output": "",
            "explanation": "单元素边界",
        })

    # ── 极值 ──
    min_val = base_inputs.get("min_val", -10**9)
    max_val = base_inputs.get("max_val", 10**9)
    cases.append({
        "input_args": _adapt_args(base_inputs, [min_val, max_val]),
        "expected_output": "",
        "explanation": f"极值边界（最小 {min_val}，最大 {max_val}）",
    })

    # ── 重复元素 ──
    cases.append({
        "input_args": _adapt_args(base_inputs, [5, 5, 5, 5]),
        "expected_output": "",
        "explanation": "全部重复元素",
    })

    # ── 负数 ──
    cases.append({
        "input_args": _adapt_args(base_inputs, [-3, -1, -7, -5]),
        "expected_output": "",
        "explanation": "负数输入",
    })

    # ── 大输入规模（快速性能嗅探） ──
    if base_inputs.get("max_n", 10**4) >= 1000:
        import random
        rnd = random.Random(42)
        large = [rnd.randint(-10**6, 10**6) for _ in range(1000)]
        cases.append({
            "input_args": _adapt_args(base_inputs, large),
            "expected_output": "",
            "explanation": "1000 元素快速性能侦测",
        })

    logger.info("Boundary: generated %d cases", len(cases))
    return cases


def _parse_constraints(
    constraints: list[str],
    example_cases: list[dict],
) -> dict:
    """从约束文本和示例用例中提取数值范围 + 输入形状。

    启发式解析（非 LLM），提取 n 的范围、值的范围、输入形状、参数数量。
    """
    result: dict[str, Any] = {
        "min_n": 1,
        "max_n": 10**4,
        "min_val": -10**9,
        "max_val": 10**9,
        "shape": "array",
        "empty_expected": "[]",
        "single_expected": "0",
        "single_val": [0],
        "n_args": 1,  # 默认 1 个参数
    }

    text = " ".join(constraints)

    # 解析 n 的范围
    for pat in [r"(\d+)\s*<=\s*n", r"n\s*<=\s*(\d+)", r"(\d+)\s*<=\s*nums"]:
        if m := re.search(pat, text):
            result["max_n"] = int(m.group(1))
            break

    for pat in [r"1\s*<=\s*n", r"(\d+)\s*<=\s*len", r"n\s*>=\s*(\d+)"]:
        if m := re.search(pat, text):
            val = int(m.group(1)) if m.lastindex == 1 else 1
            result["min_n"] = val
            break

    # 解析值范围
    if m := re.search(r"-?10\^?(\d+)", text):
        exp = int(m.group(1))
        result["min_val"] = -(10**exp)
        result["max_val"] = 10**exp

    # 从示例用例推断参数数量和返回值类型
    if example_cases:
        first = example_cases[0]
        result["n_args"] = len(first.get("input_args", []))
        # 如果有 target 参数 → 两数之和类问题
        result["has_target"] = result["n_args"] >= 2
        # 推断预期值是数组还是标量
        exp = first.get("expected_output", "")
        result["is_list_output"] = exp.startswith("[")

    return result


def _adapt_args(base: dict, vals: list) -> list[str]:
    """将 Python 值列表转为 input_args 字符串列表（ast.literal_eval 兼容）。

    根据问题签名自动适配参数数量：
    - 1 个参数（找最大值类）：只需传入数组
    - 2+ 个参数（两数之和类）：传入数组 + target
    """
    n_args = base.get("n_args", 1)
    arr_str = str(vals)

    if n_args <= 1:
        return [arr_str]
    # 多参数：第一个是数组，后面的从 vals 取或 fallback 到默认值
    args = [arr_str]
    if n_args >= 2:
        # 尝试用 vals[-1] 作为第二个参数
        target = vals[-1] if len(vals) > 0 else 0
        args.append(str(target))
    return args


# ──────────────────────────────────────────────
#  第三步：规模对抗 — LLM 描述特征 + 规则生成
# ──────────────────────────────────────────────


_SCALE_ADV_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是算法对抗测试专家。你的任务是分析一道编程题和用户的代码，
然后描述「应该在什么规模、什么分布特征下测试，才能暴露出用户代码的性能问题」。

你只输出特征描述，不输出具体数据。数据由规则引擎生成。

输出格式（JSON）：
```json
{{
    "scale_description": "描述大规模输入的特征，如'数组元素随机分布，target 靠后避免 early exit'",
    "n": 50000,
    "data_type": "int",
    "expected_weakness": "暴力 O(n²) TLE"
}}
```"""),
    ("human", """## 题目
标题: {title}
描述: {description}
约束: {constraints}

## 用户代码
```python
{code}
```

## 弱点分析
类型: {weakness_type}
详情: {weakness_detail}

请输出规模对抗的分布特征描述。"""),
])


def generate_scale_adversarial(
    problem_dict: dict,
    user_code: str,
    weakness: dict[str, Any],
) -> dict | None:
    """LLM + 规则混合生成一个规模对抗用例。

    流程（PRD §8.3）：
        1. LLM 分析题目 + 用户代码 + 弱点 → 输出"分布特征描述"
        2. 规则引擎根据特征描述拼具体数组
        3. 返回一个 test_case dict（供 run_solution 消费）

    对于单参数问题（如"找最大值"），跳过规模对抗——无法生成有意义的
    大规模测试（不知道 target 该设什么值）。

    面试考点：为什么不让 LLM 直接生成数组？
        — 10⁵ 规模的数组作为 JSON 字符串约 800KB，token 爆炸。
            — LLM 出数字分布特征比出具体数字更可靠。
            — 实际数组生成交给 Python，快且便宜。
    """
    # 检查参数数量：单参数问题跳过规模对抗
    example_cases = problem_dict.get("test_cases", [])
    if example_cases and len(example_cases[0].get("input_args", [])) <= 1:
        logger.info("Scale adversarial: single-arg problem, skipping")
        return None

    llm = get_llm("agnes", temperature=0.3)

    constraints_text = "\n".join(problem_dict.get("constraints", []))
    response = _SCALE_ADV_PROMPT | llm
    try:
        result = response.invoke({
            "title": problem_dict.get("title", ""),
            "description": problem_dict.get("description", "")[:800],
            "constraints": constraints_text,
            "code": user_code[:2000],
            "weakness_type": weakness.get("weakness_type", "unknown"),
            "weakness_detail": weakness.get("detail", ""),
        })
    except Exception as exc:
        logger.warning("Scale adversarial LLM call failed: %s", exc)
        return None

    # 解析 LLM 输出
    content = result.content if hasattr(result, "content") else str(result)
    spec = _parse_llm_json(content)

    if not spec:
        logger.warning("Could not parse LLM scale spec, using default")
        spec = {
            "scale_description": "random distribution",
            "n": 10_000,
            "data_type": "int",
        }

    # 规则引擎生成具体数组（调用 D2 的 _build_adversarial_case）
    from code_tutor_agent.sandbox.runner import _build_adversarial_case

    case = _build_adversarial_case(
        n=max(spec.get("n", 10_000), SCALE_MIN_N),
        data_type=spec.get("data_type", "int"),
        scale_description=spec.get("scale_description", "random"),
    )
    return case


def _parse_llm_json(text: str) -> dict | None:
    """从 LLM 响应中提取 JSON（容忍 markdown fences 和多余文本）。"""
    import json
    if m := re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试找 { ... } 块
        if m := re.search(r"\{.*\}", text, re.DOTALL):
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


# ──────────────────────────────────────────────
#  第四步：评审报告（Phase 3 — 复杂度 + 风格）
# ──────────────────────────────────────────────

_REVIEW_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是代码评审员。用户代码已经通过了所有测试用例（基础+对抗）。
请从以下维度评审：

1. **时间复杂度估算**：分析用户代码的复杂度，给出 Big-O 估值
2. **空间复杂度估算**：分析额外空间使用
3. **代码风格**：命名、重复代码、可读性
4. **解法归类**：暴力 / 标准 / 最优 / 骚操作

输出格式（JSON）：
```json
{{
    "time_complexity": "O(n)",
    "space_complexity": "O(n)",
    "style_rating": "good/fair/needs_improvement",
    "style_notes": ["命名良好", "但有一处重复代码建议提取"],
    "solution_category": "standard/optimal/brute",
    "summary": "一句话总结"
}}
```"""),
    ("human", """## 题目
{description}

## 用户代码
```python
{code}
```

请输出评审 JSON。"""),
])


def generate_review(problem_dict: dict, user_code: str) -> dict | None:
    """对 AC 的代码做多维评审（LLM 调用）。

    注意：评审不拦 AC — AC 就是 AC，评审只用于「告知用户」和「更新画像」。

    面试考点：为什么评审不拦 AC？
        — 出于教学原则：用户通过了测试就该得到正面反馈。
        — 评审结果影响下一题的难度（如果代码风格差，规划 Agent 可能会在同考点出加强题）。
    """
    logger.info("▶ generate_review()")
    llm = get_llm("agnes", temperature=0.2)
    try:
        result = _REVIEW_PROMPT | llm
        response = result.invoke({
            "description": problem_dict.get("description", "")[:600],
            "code": user_code[:3000],
        })
    except Exception as exc:
        logger.warning("Review LLM call failed: %s", exc)
        return None

    content = response.content if hasattr(response, "content") else str(response)
    review = _parse_llm_json(content)
    if review:
        logger.info(
            "Review → %s | style=%s | %s",
            review.get("time_complexity", "?"),
            review.get("style_rating", "?"),
            review.get("summary", "")[:80],
        )
    return review


# ──────────────────────────────────────────────
#  入口：运行完整的对抗套件
# ──────────────────────────────────────────────


class AdversarialSuite:
    """一次对抗测试的完整结果。

    包含三个阶段的输出，供 judge_node 路由决策。
    """

    def __init__(self):
        self.weakness: dict | None = None
        self.boundary_results: list[RunnerResult] = []
        self.scale_results: list[RunnerResult] = []
        self.review: dict | None = None
        self.all_passed: bool = True

    @property
    def failed_boundary(self) -> list[RunnerResult]:
        logger.info("▶ failed_boundary()")
        return [r for r in self.boundary_results if r.status != "Passed"]

    @property
    def failed_scale(self) -> list[RunnerResult]:
        logger.info("▶ failed_scale()")
        return [r for r in self.scale_results if r.status != "Passed"]

    def has_any_failure(self) -> bool:
        logger.info("▶ has_any_failure()")
        return bool(self.failed_boundary or self.failed_scale)


def run_adversarial_suite(
    problem_dict: dict,
    user_code: str,
) -> AdversarialSuite:
    """对用户 AC 代码运行完整的对抗套件。

    调用方（judge_node）只在基础判题全通过后调用此函数。

    返回的 ``AdversarialSuite`` 包含：
    - weakness: 代码弱点分析结果
    - boundary_results: 边界对抗结果列表
    - scale_results: 规模对抗结果列表
    - review: 多维评审报告（如果全部通过）
    """
    suite = AdversarialSuite()

    # ── Step 1: 代码弱点分析 ──
    suite.weakness = analyze_code_weakness(user_code)
    logger.info("Weakness: %s (conf=%.2f)", suite.weakness["weakness_type"], suite.weakness["confidence"])

    # ── Step 2: 边界对抗（纯规则生成输入，用参考解算预期值） ──
    boundary_cases = generate_boundary_cases(problem_dict)
    if not boundary_cases:
        logger.warning("No boundary cases generated — skipping boundary adversarial")
    else:
        # 用题目的 optimal_solution 计算真实 expected_output
        # 避免硬编码的预期值对不同语义的题目出错（Bug: singleNumber）
        ref_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "")
        if not ref_code:
            logger.warning("No reference solution available — skipping boundary adversarial")
        else:
            validated_cases = []
            for bc in boundary_cases:
                ref_results = run_solution(ref_code, [bc], timeout=BOUNDARY_TIMEOUT)
                if ref_results and ref_results[0].detail and ref_results[0].status == "Passed":
                    bc["expected_output"] = ref_results[0].detail
                    validated_cases.append(bc)
                else:
                    logger.warning("Boundary case failed on ref: %s — skipping", bc.get("explanation", ""))
            if not validated_cases:
                logger.warning("No valid boundary cases after ref validation — skipping")
            else:
                suite.boundary_results = run_solution(user_code, validated_cases, timeout=BOUNDARY_TIMEOUT)
                boundary_pass = all(r.status == "Passed" for r in suite.boundary_results)
                logger.info(
                    "Boundary: %d/%d passed",
                    sum(1 for r in suite.boundary_results if r.status == "Passed"),
                    len(suite.boundary_results),
                )

                if not boundary_pass:
                    suite.all_passed = False
                    # 边界挂了就不跑规模了（节约 token）
                    logger.warning("Boundary failed — skipping scale adversarial")
                    return suite

    # ── Step 3: 规模对抗（LLM + 规则） ──
    scale_case = generate_scale_adversarial(problem_dict, user_code, suite.weakness)
    if scale_case:
        suite.scale_results = run_solution(user_code, [scale_case], timeout=SCALE_ADV_TIMEOUT)
        scale_pass = all(r.status == "Passed" for r in suite.scale_results)
        logger.info("Scale: %s", "passed" if scale_pass else "failed")
        if not scale_pass:
            suite.all_passed = False
            return suite
    else:
        logger.warning("No scale case generated — skipping scale adversarial")

    # ── Step 4: 多维评审（只有全部通过才跑） ──
    suite.review = generate_review(problem_dict, user_code)

    return suite