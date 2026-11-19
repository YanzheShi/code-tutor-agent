"""出题诊断：把一次失败整理成结构化 ``Defect`` / ``Diagnosis``。

设计见 ``docs/generation-repair-agent-design.md`` §5/§6。

边界
----
- 纯本地：静态检查 + 沙箱跑参考解，**不调 LLM、不写 DB**。
- 不修改 draft（除了回填 `verified_cases`，它在 Diagnosis 里，不回写 draft）。
- 静态失败直接返回，不再跑沙箱（省一次 subprocess）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.generation.verifier import CodeVerifier

logger = logging.getLogger(__name__)

Severity = Literal["cosmetic", "quality", "blocking", "fatal"]

_SCORE = {"cosmetic": 1, "quality": 3, "blocking": 10, "fatal": 100}
_OK_STATUSES = {"Passed", "Skipped"}

# 静态 issue 文案 → (缺陷码, 级别, 允许字段)
_STATIC_RULES: tuple[tuple[str, str, Severity, tuple[str, ...]], ...] = (
    ("注释含思维链", "COT_IN_CODE", "cosmetic", ("optimal_solution", "brute_solution")),
    ("解释过长", "COT_IN_EXAMPLE", "quality", ("examples", "description")),
    ("含思维链", "COT_IN_EXAMPLE", "quality", ("examples", "description")),
    ("没有任何 return", "CODE_TRUNCATED", "fatal", ()),
    ("注释占比", "CODE_TRUNCATED", "fatal", ()),
    ("是设计类", "DESIGN_STYLE", "fatal", ()),
    ("语法错误", "COMPILE_ERROR", "blocking", ("optimal_solution", "brute_solution", "starter_code")),
    ("optimal_solution 为空", "REFERENCE_MISSING", "fatal", ()),
    ("title 为空", "STRUCT_MISSING_FIELD", "fatal", ()),
    ("description 缺失", "STRUCT_MISSING_FIELD", "fatal", ()),
    ("starter_code 为空", "STRUCT_MISSING_FIELD", "fatal", ()),
    ("starter_code 缺少", "STRUCT_MISSING_FIELD", "fatal", ()),
    ("function_signature 为空", "STRUCT_MISSING_FIELD", "fatal", ()),
)

_FIELD_PREFIXES = (
    "optimal_solution", "brute_solution", "starter_code", "description",
    "function_signature", "examples",
)


@dataclass(frozen=True)
class Defect:
    """一条可修复（或明确不可修复）的缺陷。"""

    code: str
    severity: Severity
    where: str
    message: str
    input_args: list | None = None
    stated: str | None = None
    actual: str | None = None
    allowed_fields: tuple[str, ...] = ()
    evidence: str = ""


@dataclass
class Diagnosis:
    """一次诊断的完整结果。"""

    passed: bool
    defects: list[Defect] = field(default_factory=list)
    verified_cases: list[dict] = field(default_factory=list)
    stage_reached: str = "static"
    env_failure: bool = False
    anchor_available: bool = False
    density: float = 0.0
    elapsed_ms: float = 0.0
    fingerprint: str = ""

    @property
    def blocking(self) -> list[Defect]:
        return [d for d in self.defects if d.severity in ("blocking", "fatal")]

    @property
    def only_cosmetic(self) -> bool:
        return bool(self.defects) and all(d.severity == "cosmetic" for d in self.defects)

    @property
    def score(self) -> int:
        return sum(_SCORE[d.severity] for d in self.defects)

    def codes(self) -> list[str]:
        return [d.code for d in self.defects]


# ── 静态 issue → Defect ──────────────────────────────────────────
def _defect_from_issue(issue: str) -> Defect:
    for needle, code, severity, allowed in _STATIC_RULES:
        if needle in issue:
            where = "draft"
            for prefix in _FIELD_PREFIXES:
                if issue.startswith(prefix):
                    where = prefix
                    break
            return Defect(
                code=code,
                severity=severity,
                where=where,
                message=issue,
                allowed_fields=allowed,
                evidence=issue,
            )
    return Defect("STATIC_OTHER", "blocking", "draft", issue, evidence=issue)


# ── 沙箱结果 → Defect ────────────────────────────────────────────
def _actual_of(result) -> str:
    detail = (getattr(result, "detail", "") or "").strip()
    status = getattr(result, "status", "")
    if status == "Wrong Answer":
        m = re.search(r"got=(.*)$", detail, re.S)
        return m.group(1).strip() if m else detail
    if status == "Runtime Error":
        return detail.splitlines()[0].strip() if detail else ""
    return detail


def _defect_of_case(
    index: int,
    result,
    stated: str,
) -> Defect | None:
    status = getattr(result, "status", "")
    if status in _OK_STATUSES:
        return None

    actual = _actual_of(result)
    if status == "Wrong Answer":
        code, message = "EXAMPLE_OUTPUT_MISMATCH", "示例 Output 与参考解实际输出不一致"
    elif status == "Compile Error":
        code, message = "COMPILE_ERROR", "参考解编译失败"
    elif status == "Runtime Error":
        code, message = "REFERENCE_RE", "参考解在该输入上抛异常"
    elif status == "TLE":
        code, message = "REFERENCE_TLE", "参考解在该输入上超时"
    elif status == "Judge Error":
        code, message = "ENV_UNAVAILABLE", "沙箱不可用"
    else:
        code, message = "REFERENCE_ERROR", f"参考解返回异常状态 {status}"

    # 部分示例不一致时也**不**允许改示例：否则模型可以改考卷迎合错解。
    # 示例本身写错的情况交给重采样；修复只许改参考解或 description。
    allowed = ("optimal_solution", "description")
    return Defect(
        code=code,
        severity="blocking",
        where=f"examples[{index}]",
        message=message,
        input_args=list(result.input_args) if getattr(result, "input_args", None) else None,
        stated=stated,
        actual=actual,
        allowed_fields=allowed,
        evidence=f"{status}: expected={stated!r} got={actual!r}",
    )


def _run_batch(
    sandbox,
    code: str,
    test_cases: list[dict],
    function_signature: str,
    timeout: float,
) -> list:
    """优先走批量接口；mock/旧 gateway 没有 run_batch 时退回逐例。"""
    batch = getattr(sandbox, "run_batch", None)
    if callable(batch):
        return batch(code, test_cases, function_signature=function_signature, timeout=timeout) or []
    results: list = []
    for tc in test_cases:
        results.extend(
            sandbox.run_solution(code, [tc], function_signature=function_signature) or []
        )
    return results


def _anchor_available(
    agent, draft: ProblemDraft, test_cases: list[dict], function_signature: str, timeout: float,
) -> bool:
    brute = (draft.brute_solution or "").strip()
    if not brute:
        return False
    try:
        results = _run_batch(agent.sandbox, brute, test_cases, function_signature, timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("anchor check failed: %s", exc)
        return False
    return bool(results) and all(getattr(r, "status", "") in _OK_STATUSES for r in results)


def _density(defects: list[Defect]) -> float:
    codes = {d.code for d in defects}
    if codes & {"CODE_TRUNCATED", "REFERENCE_MISSING", "DESIGN_STYLE"}:
        return 1.0
    blocking = sum(1 for d in defects if d.severity in ("blocking", "fatal"))
    if blocking >= 3:
        return 0.7
    if blocking == 2:
        return 0.4
    if blocking == 1:
        return 0.2
    return 0.0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def fingerprint(draft: ProblemDraft) -> str:
    raw = json.dumps(
        {
            "title": draft.title,
            "examples": [_norm(e) for e in (draft.examples or [])],
            "code": _norm(draft.optimal_solution),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _finish(diag: Diagnosis, draft: ProblemDraft, started: float) -> Diagnosis:
    diag.density = _density(diag.defects)
    diag.fingerprint = fingerprint(draft)
    diag.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return diag


def diagnose(draft: ProblemDraft, agent, *, timeout: float = 5.0) -> Diagnosis:
    """对 ``draft`` 做静态 + 沙箱诊断，返回纯数据 ``Diagnosis``。"""
    started = time.perf_counter()

    ok, issues = (getattr(agent, "verifier", None) or CodeVerifier()).verify(draft)
    if not ok:
        defects = [_defect_from_issue(i) for i in issues]
        return _finish(
            Diagnosis(passed=False, defects=defects, stage_reached="static"),
            draft,
            started,
        )

    function_signature = (draft.function_signature or "").strip()
    starter = (draft.starter_code or function_signature).strip()
    test_cases = agent.leetcode.parse_examples(draft.examples or [], starter)
    if not test_cases:
        return _finish(
            Diagnosis(
                passed=False,
                stage_reached="examples",
                defects=[Defect(
                    code="EXAMPLE_PARSE_FAIL",
                    severity="blocking",
                    where="examples",
                    message="示例无法解析出 Input/Output，或参数个数与函数签名不匹配",
                    allowed_fields=("examples",),
                )],
            ),
            draft,
            started,
        )

    reference = (draft.optimal_solution or draft.brute_solution or "").strip()
    if not reference:
        return _finish(
            Diagnosis(
                passed=False,
                stage_reached="examples",
                defects=[Defect(
                    code="REFERENCE_MISSING",
                    severity="fatal",
                    where="optimal_solution",
                    message="没有可执行的参考解，无法验证示例",
                )],
            ),
            draft,
            started,
        )

    try:
        results = _run_batch(agent.sandbox, reference, test_cases, function_signature, timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diagnose sandbox failed: %s", exc)
        results = []

    if not results or all(getattr(r, "status", "") == "Judge Error" for r in results):
        detail = _actual_of(results[0]) if results else "no result"
        return _finish(
            Diagnosis(
                passed=False,
                stage_reached="examples",
                env_failure=True,
                defects=[Defect(
                    code="ENV_UNAVAILABLE",
                    severity="fatal",
                    where="sandbox",
                    message="沙箱不可用（Judge0 fail-closed 或本地执行被禁用）",
                    evidence=detail,
                )],
            ),
            draft,
            started,
        )

    mismatches = [
        i for i, r in enumerate(results)
        if getattr(r, "status", "") not in _OK_STATUSES
    ]
    if not mismatches:
        verified: list[dict] = []
        for tc, result in zip(test_cases, results):
            item = dict(tc)
            item["expected_output"] = _actual_of(result)
            verified.append(item)

        # 延迟 import：quality 依赖本模块的 Defect，模块级导入会成环。
        from code_tutor_agent.generation.quality import check_quality, quality_gate_enabled

        if quality_gate_enabled():
            quality_defects, anchor = check_quality(
                agent, draft, test_cases, results, timeout=timeout,
            )
            if quality_defects:
                return _finish(
                    Diagnosis(
                        passed=False,
                        defects=quality_defects,
                        stage_reached="quality",
                        anchor_available=anchor,
                    ),
                    draft,
                    started,
                )

        return _finish(
            Diagnosis(passed=True, verified_cases=verified, stage_reached="examples"),
            draft,
            started,
        )

    defects = []
    for i in mismatches:
        defect = _defect_of_case(i, results[i], str(test_cases[i].get("expected_output", "")))
        if defect is not None:
            defects.append(defect)
    anchor = _anchor_available(agent, draft, test_cases, function_signature, timeout)
    return _finish(
        Diagnosis(
            passed=False,
            defects=defects,
            stage_reached="examples",
            anchor_available=anchor,
        ),
        draft,
        started,
    )
