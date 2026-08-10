"""CodeVerifier — 出题验证器（三查，设计 §9）。

V1 节点对应物：前两项提炼自 ``agent_problem.verify_problem``（L125，十项检查），
「无 CoT 泄漏」为新增第三项。

三查：
1) 结构完整：title/description/starter_code/签名 非空 + class Solution
2) 编译：``compile()``（与 verify_problem 现有检查一致，等价 py_compile）
3) 无 CoT 泄漏：扫描 description 与 optimal/brute 代码注释中的思考痕迹
   （仿 constitutional_guard._r09/_r10 的保守扫描风格）
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# CoT 泄漏关键词：拦截真正的思维链痕迹，避免误伤正常描述用语
# （"这道题"、"其实"、"经典" 等正常用语不在列，与 agent_problem.verify_problem 同口径）
_COT_KEYWORDS = (
    "让我们", "再试一个", "再试", "一步一步", "我们先", "我们来试试",
    "思考过程", "推理如下", "逐步分析", "接下来考虑",
)
# 代码注释中出现的思考痕迹（"思考"性话术出现在题解里说明 LLM 泄了推理链）
_COT_COMMENT_PATTERNS = (
    r"#.*(逐步|思考|推理|我们来试)",
    r"#.*(先.*再.*最后)",
)


class CodeVerifier:
    """对 ``ProblemDraft`` 做结构 / 编译 / 无 CoT 三查。"""

    def verify(self, draft) -> tuple[bool, list[str]]:
        """返回 (是否通过, 问题列表)；不修改 draft。"""
        issues: list[str] = []
        self._check_structure(draft, issues)
        self._check_compiles(draft, issues)
        self._check_no_cot(draft, issues)
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
        elif "class Solution" not in starter or "def " not in starter:
            issues.append("starter_code 缺少 class Solution / def")
        if not (draft.function_signature or "").strip():
            issues.append("function_signature 为空")
        # 仅原创通道强制要求参考解（导入题参考解由后台补，失败不阻断出题）
        if not draft.from_leetcode and not (draft.optimal_solution or "").strip():
            issues.append("optimal_solution 为空")

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

    # ── 3) 无 CoT 泄漏 ──
    def _check_no_cot(self, draft, issues: list[str]) -> None:
        desc = draft.description or ""
        if any(kw in desc for kw in _COT_KEYWORDS):
            issues.append("description 含思维链痕迹")
        for field_name in ("optimal_solution", "brute_solution"):
            code = getattr(draft, field_name, "") or ""
            for pat in _COT_COMMENT_PATTERNS:
                if re.search(pat, code, re.IGNORECASE):
                    issues.append(f"{field_name} 注释含思维链痕迹")
                    break
