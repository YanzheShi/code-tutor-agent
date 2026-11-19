"""修复回路（P1：工具关闭；P2 打开工具箱）。

设计见 ``docs/generation-repair-agent-design.md`` §4/§9。

三件事在这里收口：
- **分诊**：``decide`` 决定 save / clean / repair / resample / fallback；
- **有界循环**：轮数、墙钟、无进展、指纹重复，任一命中即退出；
- **反降级**：删示例、放宽约束、改题意、退化解、越权改字段，一律作废该轮。

不落库、不碰降级链；这些仍由 ``ProblemGenerationAgent.run`` 负责。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from code_tutor_agent.generation.diagnosis import Diagnosis
from code_tutor_agent.generation.state import (
    GenEvent,
    ProblemDraft,
    ProgressSink,
)

logger = logging.getLogger(__name__)

_TITLE_EXEMPT_CODE = "TITLE_DUPLICATE"
_CLEAN_CODE = "COT_IN_CODE"

# 反降级只比较这些字段（test_cases 由诊断回填，不参与）
_COMPARED_FIELDS = (
    "topic", "difficulty", "title", "description", "examples",
    "constraints", "starter_code", "optimal_solution",
    "brute_solution", "function_signature",
)

_LEN_RE = re.compile(r"length\s*<=\s*(\d+(?:\^\d+)?)", re.IGNORECASE)
_VAL_RE = re.compile(r"\[\w+\]\s*<=\s*(\d+(?:\^\d+)?)", re.IGNORECASE)
_OUTPUT_RE = re.compile(r"(?:Output|输出)\s*[:：]\s*(.+)", re.IGNORECASE)


def repair_enabled() -> bool:
    raw = os.getenv("CTA_GEN_REPAIR_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _norm_one(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_compact(value) -> str:
    """列表项（示例/约束）忽略全部空白，避免模型重排版被判成「改动」。"""
    return re.sub(r"\s+", "", str(value or ""))


def _norm(value):
    if isinstance(value, list):
        return [_norm_compact(v) for v in value]
    return _norm_one(value)


def _parse_num(token: str) -> int:
    token = token.replace(",", "")
    if "^" in token:
        base, exp = token.split("^", 1)
        return int(base) ** int(exp)
    return int(token)


def _max_bound(constraints: list[str], pattern: re.Pattern) -> int | None:
    best = None
    for item in constraints or []:
        for match in pattern.finditer(item or ""):
            try:
                n = _parse_num(match.group(1))
            except ValueError:
                continue
            best = n if best is None else max(best, n)
    return best


def _expected_set(examples: list[str]) -> set[str]:
    out: set[str] = set()
    for example in examples or []:
        match = _OUTPUT_RE.search(example or "")
        if match:
            out.add(match.group(1).strip())
    return out


# ── 分诊 ────────────────────────────────────────────────────────
def decide(diagnosis: Diagnosis, draft: ProblemDraft, attempt: int) -> str:
    """确定性分诊：返回 save / clean / repair / resample / fallback。"""
    if diagnosis.passed:
        return "save"
    if diagnosis.env_failure:
        return "fallback"

    codes = set(diagnosis.codes())
    if codes & {"CODE_TRUNCATED", "DESIGN_STYLE", "REFERENCE_MISSING"}:
        return "resample"          # 缺陷密度接近 100%，重抽更便宜
    if diagnosis.only_cosmetic and _CLEAN_CODE in codes:
        return "clean"
    if diagnosis.density >= 0.6:
        return "resample"
    if diagnosis.anchor_available or len(diagnosis.blocking) <= 2:
        return "repair"
    return "resample"


# ── 反降级闸 ────────────────────────────────────────────────────
def _changed_fields(old: ProblemDraft, new: ProblemDraft) -> list[str]:
    changed: list[str] = []
    for name in _COMPARED_FIELDS:
        if _norm(getattr(old, name, None)) != _norm(getattr(new, name, None)):
            changed.append(name)
    return changed


def _restore_readonly(old: ProblemDraft, new: ProblemDraft, codes: set[str]) -> list[str]:
    """把只读字段强制回滚到旧值，返回被回滚的字段名。

    比「改了就拒绝整轮」温和：模型顺手改了 examples，但代码确实修对了时，
    回滚示例后仍可通过；如果它只是改考卷迎合错解，回滚后诊断依旧失败。
    """
    reverted: list[str] = []
    if list(new.examples or []) != list(old.examples or []):
        new.examples = list(old.examples or [])
        reverted.append("examples")
    if new.topic != old.topic:
        new.topic = old.topic
        reverted.append("topic")
    if new.difficulty != old.difficulty:
        new.difficulty = old.difficulty
        reverted.append("difficulty")
    if new.title != old.title and _TITLE_EXEMPT_CODE not in codes:
        new.title = old.title
        reverted.append("title")
    return reverted


def anti_degradation(
    old: ProblemDraft,
    new: ProblemDraft,
    *,
    defects: list,
) -> list[str]:
    """返回违规项（空 = 通过）。判据是「信息量没有减少」，不是「字数没缩水」。"""
    violations: list[str] = []
    codes = {d.code for d in defects}

    if new.topic != old.topic:
        violations.append("topic 被修改")
    if new.difficulty != old.difficulty:
        violations.append("difficulty 被修改")
    if new.title != old.title and _TITLE_EXEMPT_CODE not in codes:
        violations.append("title 被修改（仅 TITLE_DUPLICATE 允许）")

    old_examples = list(old.examples or [])
    new_examples = list(new.examples or [])
    if len(new_examples) < len(old_examples):
        violations.append(f"示例数减少（{len(old_examples)} → {len(new_examples)}）")
    if len(_norm_one(new.description)) < 0.7 * len(_norm_one(old.description)):
        violations.append("description 明显缩短（< 原文 70%）")

    old_len = _max_bound(old.constraints, _LEN_RE)
    new_len = _max_bound(new.constraints, _LEN_RE)
    if old_len is not None and new_len is not None and new_len > old_len:
        violations.append(f"长度约束被放宽（{old_len} → {new_len}）")
    old_val = _max_bound(old.constraints, _VAL_RE)
    new_val = _max_bound(new.constraints, _VAL_RE)
    if old_val is not None and new_val is not None and new_val > old_val:
        violations.append(f"数值约束被放宽（{old_val} → {new_val}）")

    if not (new.optimal_solution or "").strip():
        violations.append("optimal_solution 为空")
    elif _is_stub(new.optimal_solution):
        violations.append("optimal_solution 是空壳")

    old_expected = _expected_set(old_examples)
    new_expected = _expected_set(new_examples)
    if len(old_expected) > 1 and len(new_expected) == 1:
        violations.append("所有示例期望被改成同一个值（退化解）")

    allowed: set[str] = set()
    for defect in defects:
        allowed.update(defect.allowed_fields)
    if _TITLE_EXEMPT_CODE in codes:
        allowed.add("title")
    if allowed:
        changed = _changed_fields(old, new)
        illegal = [f for f in changed if f not in allowed]
        if illegal:
            violations.append(f"修改了未授权字段：{', '.join(illegal)}")

    return violations


def _is_stub(code: str) -> bool:
    """延迟导入，避免 generation/ 包在 import 时拉起 langchain。"""
    try:
        from code_tutor_agent.agents.agent_problem import _is_stub_solution

        return _is_stub_solution(code)
    except Exception:  # noqa: BLE001
        return False


# ── 预算 / 结果 ─────────────────────────────────────────────────
@dataclass
class RepairBudget:
    max_rounds: int = 2
    per_round_s: float = 90.0
    total_deadline_ts: float = 0.0
    max_tool_calls: int = 6
    max_tokens: int = 30_000
    max_no_progress: int = 2

    @classmethod
    def from_env(cls, ctx=None) -> "RepairBudget":
        total_s = float(os.getenv("CTA_GEN_REPAIR_TOTAL_S", "180"))
        deadline = time.time() + total_s
        ctx_deadline = getattr(ctx, "deadline_ts", None)
        if ctx_deadline:
            deadline = min(deadline, float(ctx_deadline) - 15.0)
        return cls(
            max_rounds=int(os.getenv("CTA_GEN_REPAIR_MAX_ROUNDS", "2")),
            per_round_s=float(os.getenv("CTA_GEN_REPAIR_ROUND_S", "90")),
            total_deadline_ts=deadline,
            max_tool_calls=int(os.getenv("CTA_GEN_REPAIR_MAX_TOOL_CALLS", "6")),
            max_tokens=int(os.getenv("CTA_GEN_REPAIR_MAX_TOKENS", "30000")),
        )


@dataclass
class RepairRound:
    round: int
    before: list[str]
    after: list[str]
    before_score: int
    after_score: int
    changed: list[str]
    violations: list[str]
    tools: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    llm_ms: float = 0.0
    outcome: str = "accepted"

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "before": self.before,
            "after": self.after,
            "score": [self.before_score, self.after_score],
            "changed": self.changed,
            "violations": self.violations,
            "tools": self.tools,
            "reverted": self.reverted,
            "llm_ms": round(self.llm_ms, 1),
            "outcome": self.outcome,
        }


@dataclass
class RepairOutcome:
    draft: ProblemDraft | None
    diagnosis: Diagnosis | None
    rounds: list[RepairRound] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.draft is not None


class RepairSession:
    """一次出题的修复会话（有界、同步、无共享状态）。"""

    def __init__(
        self,
        agent,
        budget: RepairBudget,
        sink: ProgressSink,
        *,
        tools=None,
    ) -> None:
        self.agent = agent
        self.budget = budget
        self.sink = sink
        self.tools = tools

    def run(self, draft: ProblemDraft, diagnosis: Diagnosis) -> RepairOutcome:
        rounds: list[RepairRound] = []
        prev = diagnosis
        seen = {prev.fingerprint}
        no_progress = 0

        for index in range(1, self.budget.max_rounds + 1):
            if time.time() > self.budget.total_deadline_ts:
                return RepairOutcome(None, prev, rounds, "deadline")

            self.sink.event(GenEvent(
                "progress",
                f"🔧 第 {index} 轮修复（缺陷：{', '.join(prev.codes()) or '—'}）…",
            ))
            started = time.perf_counter()
            tools = None
            try:
                from code_tutor_agent.generation.toolbox import (
                    ToolBox,
                    needs_tools,
                    toolbox_enabled,
                )

                if toolbox_enabled() and needs_tools(prev):
                    tools = ToolBox(
                        self.agent, draft, prev,
                        max_calls=self.budget.max_tool_calls,
                        deadline=min(
                            time.time() + self.budget.per_round_s,
                            self.budget.total_deadline_ts,
                        ),
                    ).tools()
            except Exception as exc:  # noqa: BLE001
                logger.warning("toolbox init failed (repair without tools): %s", exc)

            try:
                revision = self.agent.llm.repair_problem(
                    draft,
                    prev,
                    tools=tools,
                    deadline=min(
                        time.time() + self.budget.per_round_s,
                        self.budget.total_deadline_ts,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("repair LLM failed: %s", exc)
                revision = None
            llm_ms = (time.perf_counter() - started) * 1000

            if revision is None:
                rounds.append(RepairRound(
                    index, prev.codes(), [], prev.score, prev.score,
                    [], ["repair_problem 返回空"], llm_ms=llm_ms, outcome="llm_error",
                ))
                return RepairOutcome(None, prev, rounds, "llm_error")

            codes = set(prev.codes())
            reverted = _restore_readonly(draft, revision.draft, codes)
            changed = _changed_fields(draft, revision.draft)
            tool_names = [c.get("tool", "") for c in (revision.tool_calls or [])]
            violations = anti_degradation(draft, revision.draft, defects=prev.defects)
            if violations:
                rounds.append(RepairRound(
                    index, prev.codes(), [], prev.score, prev.score,
                    changed, violations, tools=tool_names, reverted=reverted,
                    llm_ms=llm_ms, outcome="rejected_degraded",
                ))
                self.sink.event(GenEvent("warning", f"修复被反降级闸拒绝：{violations[0]}"))
                return RepairOutcome(None, prev, rounds, "rejected_degraded")

            new_diag = self.agent.diagnose(revision.draft)
            outcome = "passed" if new_diag.passed else "accepted"
            rounds.append(RepairRound(
                index, prev.codes(), new_diag.codes(), prev.score, new_diag.score,
                changed, [], tools=tool_names, reverted=reverted,
                llm_ms=llm_ms, outcome=outcome,
            ))

            if new_diag.passed:
                return RepairOutcome(revision.draft, new_diag, rounds, "repaired")
            if new_diag.env_failure:
                return RepairOutcome(None, new_diag, rounds, "env")
            if new_diag.fingerprint in seen:
                return RepairOutcome(None, new_diag, rounds, "fingerprint")
            seen.add(new_diag.fingerprint)

            if new_diag.score >= prev.score:
                no_progress += 1
            else:
                no_progress = 0
            if no_progress >= self.budget.max_no_progress:
                return RepairOutcome(None, new_diag, rounds, "no_progress")

            draft, prev = revision.draft, new_diag

        return RepairOutcome(None, prev, rounds, "max_rounds")
