"""CodeVerifier — 出题验证器（五查，设计 §9）。

V1 节点对应物：前两项提炼自 ``agent_problem.verify_problem``（L125，十项检查），
「无 CoT 泄漏」为新增第三项；「静态代码质量」为 2026-11-19 新增第四项；
「题型与数据结构一致性」为 2026-11-21 新增第五项。

五查：
1) 结构完整：title/description/starter_code/签名 非空 + class Solution
2) 编译：``compile()``（与 verify_problem 现有检查一致，等价 py_compile）
3) 静态代码质量（``guards.code_quality``）：入口方法无 return、注释占比过高
   —— 专治「输出被截断 / 用思考注释填满预算」的假合格题解
4) 无 CoT 泄漏：扫描 description、examples（含解释）与参考解代码注释中的
   思考痕迹（中英文，口径集中在 guards.code_quality）
5) 题型一致：链表题必须用 ListNode、树题必须用 TreeNode（``guards.topic_guard``）
"""

from __future__ import annotations

import logging

from code_tutor_agent.guards.code_quality import (
    CN_COT_KEYWORDS,  # noqa: F401  # 兼容旧引用
    code_cot_issues,
    code_quality_issues,
    example_issues,
    text_cot_issues,
)
from code_tutor_agent.guards.design_guard import is_design_style_code
from code_tutor_agent.guards.topic_guard import topic_structure_issues

logger = logging.getLogger(__name__)


class CodeVerifier:
    """对 ``ProblemDraft`` 做结构 / 编译 / 静态质量 / 无 CoT / 题型一致五查。"""

    def verify(self, draft) -> tuple[bool, list[str]]:
        """返回 (是否通过, 问题列表)；不修改 draft。"""
        issues: list[str] = []
        self._check_structure(draft, issues)
        self._check_compiles(draft, issues)
        self._check_code_quality(draft, issues)
        self._check_no_cot(draft, issues)
        self._check_topic_structure(draft, issues)
        return (not issues, issues)

    # ── 1) 结构完整 ──
    def _check_structure(self, draft, issues: list[str]) -> None:
        if not (draft.title or "").strip():
            issues.append("title 为空")
        if len((draft.description or "").strip()) < 10:
            issues.append("description 缺失或过短")
        starter = (draft.starter_code or "").strip()
        if not starter:
            issues.append("starter_code 为空")
        elif is_design_style_code(starter):
            issues.append("starter_code 是设计类模板（主类不是 Solution），暂不支持设计类题目")
        elif "class Solution" not in starter or "def " not in starter:
            issues.append("starter_code 缺少 class Solution / def")
        if not (draft.function_signature or "").strip():
            issues.append("function_signature 为空")
        # 仅原创通道强制要求参考解（导入题参考解由后台补，失败不阻断出题）
        if not draft.from_leetcode and not (draft.optimal_solution or "").strip():
            issues.append("optimal_solution 为空")
        elif (draft.optimal_solution or "").strip() and is_design_style_code(draft.optimal_solution):
            issues.append("optimal_solution 是设计类代码（主类不是 Solution），暂不支持设计类题目")

    # ── 2) 编译 ──
    def _check_compiles(self, draft, issues: list[str]) -> None:
        for field_name in ("optimal_solution", "brute_solution"):
            code = (getattr(draft, field_name, "") or "").strip()
            if not code:
                continue
            try:
                compile(code, f"<{field_name}>", "exec")
            except SyntaxError as exc:
                issues.append(f"{field_name} 语法错误: {exc}")

    # ── 3) 静态代码质量（截断 / 注释填充）──
    def _check_code_quality(self, draft, issues: list[str]) -> None:
        for field_name in ("optimal_solution", "brute_solution"):
            code = (getattr(draft, field_name, "") or "").strip()
            if code:
                issues.extend(code_quality_issues(code, field=field_name))

    # ── 4) 无 CoT 泄漏（description / examples / 代码注释）──
    def _check_no_cot(self, draft, issues: list[str]) -> None:
        issues.extend(text_cot_issues(draft.description or "", where="description"))
        for idx, example in enumerate(getattr(draft, "examples", None) or []):
            issues.extend(example_issues(str(example), where=f"examples[{idx}]"))
        for field_name in ("optimal_solution", "brute_solution"):
            code = getattr(draft, field_name, "") or ""
            if code.strip():
                issues.extend(code_cot_issues(code, field=field_name))

    # ── 5) 题型与数据结构一致 ──
    def _check_topic_structure(self, draft, issues: list[str]) -> None:
        issues.extend(topic_structure_issues(
            getattr(draft, "topic", "") or "",
            starter_code=getattr(draft, "starter_code", "") or "",
            function_signature=getattr(draft, "function_signature", "") or "",
            optimal_solution=getattr(draft, "optimal_solution", "") or "",
        ))
