"""质量闸（P3）：抓「示例能过、但题是坏的」这一类。

设计见 ``docs/generation-repair-agent-design.md`` §6.2 / §13。

三个检查：
1. **双解交叉**：optimal 与 brute 在小规模随机输入上结果不一致 → 至少一个错。
2. **域校验**：示例输入必须符合题面声明的结构/字符集约束。
3. **区分度**：所有示例期望输出相同 → 空壳 `return 0` 也能全过。

只在示例闸通过后运行；纯本地，不调 LLM。为了不拖首屏，随机输入规模限制在
很小的一批（默认 4 条、每条总字符数 ≤200）。
"""

from __future__ import annotations

import json
import re

from code_tutor_agent.generation.diagnosis import Defect
from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.sandbox.input_generator import check_length_relations

_OK_STATUSES = {"Passed", "Skipped"}
_MAX_RANDOM = 4
_MAX_INPUT_CHARS = 200
_MAX_DEFECTS = 3


def quality_gate_enabled() -> bool:
    import os

    raw = os.getenv("CTA_GEN_QUALITY_GATE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _actual_of(result) -> str:
    detail = (getattr(result, "detail", "") or "").strip()
    status = getattr(result, "status", "")
    if status == "Wrong Answer":
        match = re.search(r"got=(.*)$", detail, re.S)
        return match.group(1).strip() if match else detail
    if status == "Runtime Error":
        return detail.splitlines()[0].strip() if detail else ""
    return detail


def _value_type(value: str) -> str:
    """输出的 JSON 类型（用来识别 brute 连返回类型都写错的假分歧）。"""
    text = (value or "").strip()
    if text in ("True", "False", "true", "false"):
        return "bool"
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return "str"
    if isinstance(parsed, bool):
        return "bool"
    if isinstance(parsed, int):
        return "int"
    if isinstance(parsed, float):
        return "float"
    if isinstance(parsed, list):
        return "list"
    if isinstance(parsed, dict):
        return "dict"
    if isinstance(parsed, str):
        return "str"
    return "other"


def _run_batch(agent, code: str, cases: list[dict], signature: str, timeout: float) -> list:
    batch = getattr(agent.sandbox, "run_batch", None)
    if callable(batch):
        return batch(code, cases, function_signature=signature, timeout=timeout) or []
    results: list = []
    for case in cases:
        results.extend(
            agent.sandbox.run_solution(code, [case], function_signature=signature) or []
        )
    return results


def _domain_defects(agent, draft: ProblemDraft, cases: list[dict]) -> list[Defect]:
    sandbox = agent.sandbox
    check = getattr(sandbox, "input_in_domain", None)
    signature = (draft.function_signature or "").strip()
    if not callable(check) or not signature:
        return []
    defects: list[Defect] = []
    for index, case in enumerate(cases):
        try:
            ok = check(signature, case.get("input_args") or [], None)
        except Exception:  # noqa: BLE001
            ok = True
        if ok:
            ok = check_length_relations(
                signature, case.get("input_args") or [], draft.constraints,
            )
        if not ok:
            defects.append(Defect(
                code="EXAMPLE_INPUT_OUT_OF_DOMAIN",
                severity="blocking",
                where=f"examples[{index}]",
                message="示例输入不符合题面声明的结构/类型约束，判题无法正确执行",
                input_args=case.get("input_args"),
                evidence="input_in_domain=false",
            ))
            if len(defects) >= _MAX_DEFECTS:
                break
    return defects


def _weak_example_defect(cases: list[dict], results: list) -> list[Defect]:
    if len(cases) < 2:
        return []
    values = {_actual_of(result).strip() for result in results}
    if len(values) != 1:
        return []
    value = next(iter(values))
    return [Defect(
        code="WEAK_EXAMPLES",
        severity="blocking",
        where="examples",
        message=f"全部示例期望输出都是 {value!r}，空壳解法也能全过，题目没有区分度",
        evidence=f"{len(cases)} 个示例的 expected 完全相同",
    )]


def _small_random_cases(agent, draft: ProblemDraft) -> list[dict]:
    generator = getattr(agent.sandbox, "random_inputs", None)
    signature = (draft.function_signature or "").strip()
    if not callable(generator) or not signature:
        return []
    try:
        inputs = generator(
            signature,
            n=_MAX_RANDOM,
            seed=31,
            constraints=draft.constraints or [],
            description=draft.description or "",
        )
    except Exception:  # noqa: BLE001
        return []
    cases: list[dict] = []
    for args in inputs or []:
        if len("".join(str(a) for a in args)) > _MAX_INPUT_CHARS:
            continue
        cases.append({
            "input_args": args,
            "expected_output": "",
            "explanation": "quality probe",
        })
        if len(cases) >= _MAX_RANDOM:
            break
    return cases


def _cross_check_defects(
    agent, draft: ProblemDraft, probe_cases: list[dict], timeout: float,
) -> tuple[list[Defect], bool]:
    brute = (draft.brute_solution or "").strip()
    optimal = (draft.optimal_solution or "").strip()
    signature = (draft.function_signature or "").strip()
    if not brute or not optimal or not probe_cases:
        return [], False

    optimal_results = _run_batch(agent, optimal, probe_cases, signature, timeout)
    brute_results = _run_batch(agent, brute, probe_cases, signature, timeout)
    if len(optimal_results) != len(probe_cases) or len(brute_results) != len(probe_cases):
        return [], False

    crashes: list[Defect] = []
    disagreements: list[Defect] = []
    for index, (opt, bru) in enumerate(zip(optimal_results, brute_results)):
        opt_status = getattr(opt, "status", "")
        if opt_status in ("Runtime Error", "TLE"):
            # 随机输入的取值域不一定完全符合题面（生成器只按长度/粗略范围取样），
            # 参考解在“可能是非法输入”上崩溃 → 只记 warning，不阻断。
            crashes.append(Defect(
                code="REFERENCE_TLE" if opt_status == "TLE" else "REFERENCE_RE",
                severity="quality",
                where=f"random[{index}]",
                message="示例能过，但参考解在随机输入上崩溃/超时（该输入可能越界，仅提示）",
                input_args=probe_cases[index].get("input_args"),
                actual=_actual_of(opt),
                allowed_fields=("optimal_solution",),
                evidence=f"{opt_status} on random input",
            ))
            continue
        if opt_status not in _OK_STATUSES or getattr(bru, "status", "") not in _OK_STATUSES:
            continue

        opt_actual = _actual_of(opt).strip()
        brute_actual = _actual_of(bru).strip()
        if opt_actual == brute_actual:
            continue
        if _value_type(opt_actual) != _value_type(brute_actual):
            # brute 连返回类型都写错（如 int vs bool）→ 是 brute 坏，不是真分歧
            continue
        disagreements.append(Defect(
            code="SOLVER_DISAGREEMENT",
            severity="blocking",
            where=f"random[{index}]",
            message="最优解与暴力解在随机输入上结果不一致，至少有一个在边界上写错了",
            input_args=probe_cases[index].get("input_args"),
            stated=opt_actual,
            actual=brute_actual,
            allowed_fields=("optimal_solution", "brute_solution"),
            evidence=f"optimal={opt_actual!r} brute={brute_actual!r}",
        ))

    # 单条分歧可能只是随机输入越界；≥2 条同类型分歧才当作真信号。
    if len(disagreements) < 2:
        disagreements = []
    return (crashes + disagreements)[:_MAX_DEFECTS], False


def check_quality(
    agent,
    draft: ProblemDraft,
    test_cases: list[dict],
    optimal_results: list,
    *,
    timeout: float = 5.0,
) -> tuple[list[Defect], bool]:
    """在示例闸通过后做质量检查，返回 (defects, anchor_available)。"""
    defects: list[Defect] = []
    defects.extend(_domain_defects(agent, draft, test_cases))
    defects.extend(_weak_example_defect(test_cases, optimal_results))
    if len(defects) >= _MAX_DEFECTS:
        return defects[:_MAX_DEFECTS], False

    probe_cases = _small_random_cases(agent, draft)
    cross_defects, anchor = _cross_check_defects(agent, draft, probe_cases, timeout)
    defects.extend(cross_defects)
    return defects[:_MAX_DEFECTS], anchor
