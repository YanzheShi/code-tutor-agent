"""代码质量静态闸：截断 / 注释填充 / 思维链泄漏（2026-11-19）。

来源
----
`badcase/generation/bc-gen-2026-11-18-02-truncated-cot.json`：
LLM 把输出预算耗在英文思考注释上，`class Solution` 只写到 `prefix.append(s)`
就断了，函数**没有 return**，所有输入恒返回 None。它却通过了原有静态校验：

- `_is_stub_solution` 认为「有赋值 / 循环」就是有真实逻辑，不看有没有 return；
- CoT 检查只认中文关键词，且只扫 `description` 与代码注释，不扫 `examples`。

结果白跑一次沙箱 + 一次重采样，日志里只留下一句 “Wrong Answer”。

职责边界
--------
纯静态：不执行代码、不调 LLM、不碰 DB。供两处共用：

- ``agents.agent_problem.verify_problem`` —— LLM 出题后的第一道闸（省掉沙箱）；
- ``generation.verifier.CodeVerifier`` —— generation 包的统一校验入口。

阈值刻意宽松：**宁可漏，不可误伤正常题解**。真正的正确性仍由示例沙箱校验负责。
"""

from __future__ import annotations

import ast
import re

# ── 思维链关键词 ──────────────────────────────────────────────
# 中文：沿用 generation/verifier.py 原有口径（避免行为漂移），补几个“自我怀疑”信号。
CN_COT_KEYWORDS = (
    "让我们", "再试一个", "再试", "一步一步", "我们先", "我们来试试",
    "思考过程", "推理如下", "逐步分析", "接下来考虑",
    # 2026-11-19 补：模型在示例解释里推翻自己时的口头语
    "等一下", "等等，", "让我重新", "重新推导", "为什么示例", "不对，", "我算错",
)

# 英文：截断样本里的真实用词。只收高精度短语，避免误伤正常英文说明。
EN_COT_PATTERNS = (
    r"\blet me\b",
    r"\blet's (?:think|look|use|re-?check|try|see)\b",
    r"\blet us\b",
    r"\bwait\s*[,，]",
    r"\bhold on\b",
    r"\bhmm+\b",
    r"\bactually\s*[,，]",
    r"\bi think\b",
    r"\bmaybe we\b",
    r"\bso the formula\b",
    r"\bwhy (?:is|does) (?:the )?(?:output|example)\b",
    r"\bthat doesn'?t match\b",
)

# 代码注释里出现这些，说明模型把推理写进了题解
_COMMENT_COT_HINTS = ("逐步", "思考", "推理", "我们来试", "先.*再.*最后")

# 注释占比超过该比例、且可执行行不足 _MIN_EXEC_LINES，判为「用注释填充输出」
_COMMENT_RATIO = 0.70
_MIN_EXEC_LINES = 10

# 示例「解释」超过该长度，判为疑似把推理过程写进题面（正常 1~2 句，几十字）
_MAX_EXPLANATION_CHARS = 500

_EXPLANATION_SPLIT = re.compile(r"(?:解释|Explanation|Explain)\s*[:：]", re.IGNORECASE)


def _find_solution_class(tree: ast.Module) -> ast.ClassDef | None:
    """找到顶层的 ``class Solution``（含被 if/try 包裹的情况）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Solution":
            return node
    return None


def _public_methods(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """``class Solution`` 的直接方法，按名字排序。

    与判题 harness 的选法对齐（``inspect.getmembers`` 返回按名字排序的结果，
    取第一个非下划线方法），避免「闸门认为的入口」和「实际跑的入口」不是同一个。
    """
    methods = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    public = [m for m in methods if not m.name.startswith("_")]
    return sorted(public or methods, key=lambda m: m.name)


def _has_return(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """函数体内是否有 return（不把嵌套函数/lambda 里的 return 算作自己的）。"""
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _comment_stats(code: str) -> tuple[int, int, float]:
    """返回 (非空行数, 注释行数, 注释占比)。"""
    lines = [ln for ln in code.splitlines() if ln.strip()]
    comments = [ln for ln in lines if ln.lstrip().startswith("#")]
    ratio = len(comments) / len(lines) if lines else 0.0
    return len(lines), len(comments), ratio


def code_quality_issues(code: str, *, field: str) -> list[str]:
    """静态检查一段 ``class Solution`` 代码，返回问题列表（空 = 通过）。

    覆盖三类：
    1. 入口方法没有 return —— 典型是输出被截断，函数只写了一半；
    2. 注释占比过高且可执行代码过少 —— 模型用思考过程注释填满输出预算；
    3. （由 :func:`code_cot_issues` 单独负责）注释里的思维链痕迹。

    语法错误不在这里报（由 ``compile`` / ``check_compile`` 负责，避免重复告警）；
    主类不是 Solution 也不在这里报（由 ``guards.design_guard`` 负责）。
    """
    code = (code or "").strip()
    if not code:
        return []

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    cls = _find_solution_class(tree)
    if cls is None:
        return []

    issues: list[str] = []
    methods = _public_methods(cls)
    if not methods:
        issues.append(f"{field}: class Solution 中没有任何方法")
    else:
        entry = methods[0]
        if not _has_return(entry):
            issues.append(
                f"{field}: Solution.{entry.name}() 没有任何 return，"
                "所有输入都会返回 None（疑似输出被截断）"
            )

    total, comments, ratio = _comment_stats(code)
    exec_lines = total - comments
    if ratio > _COMMENT_RATIO and exec_lines < _MIN_EXEC_LINES:
        issues.append(
            f"{field}: 注释占比 {ratio:.0%} 且可执行代码仅 {exec_lines} 行，"
            "疑似用思考过程注释填充输出"
        )
    return issues


def _first_cot_hit(text: str) -> str | None:
    for kw in CN_COT_KEYWORDS:
        if kw in text:
            return kw
    for pat in EN_COT_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def text_cot_issues(text: str, *, where: str) -> list[str]:
    """文本（description / 示例 / 解释）里的思维链痕迹。"""
    hit = _first_cot_hit(text or "")
    if hit is None:
        return []
    return [f"{where} 含思维链痕迹（命中：{hit}）"]


def code_cot_issues(code: str, *, field: str) -> list[str]:
    """代码注释里的思维链痕迹（中英文）。"""
    code = code or ""
    if not code.strip():
        return []
    for line in code.splitlines():
        s = line.strip()
        if not s.startswith("#"):
            continue
        body = s.lstrip("#").strip()
        hit = _first_cot_hit(body)
        if hit is not None:
            return [f"{field} 注释含思维链痕迹（命中：{hit}）"]
        if any(re.search(pat, body) for pat in _COMMENT_COT_HINTS):
            return [f"{field} 注释含思维链痕迹（逐步/思考/推理）"]
    return []


def example_issues(example: str, *, where: str) -> list[str]:
    """单个示例的静态检查：思维链痕迹 + 解释过长（疑似推理过程）。"""
    example = example or ""
    issues = text_cot_issues(example, where=where)

    parts = _EXPLANATION_SPLIT.split(example, maxsplit=1)
    if len(parts) == 2:
        explanation = parts[1].strip()
        if len(explanation) > _MAX_EXPLANATION_CHARS:
            issues.append(
                f"{where} 的解释过长（{len(explanation)} 字），疑似把推理过程写进了题面"
            )
    return issues
