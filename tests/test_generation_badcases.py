"""出题 badcase 回归样本（2026-11-18 同一会话三次原创题失败）。

这些样本锁的不是「业务代码当前行为」，而是**失败可复现**：

- 同一份 problem 用同一套 parser + runner 复跑，必须复现 `observed.offline_full_scan`
  里的 `status` / `actual`。parser 或 runner 行为漂移会在这里红灯。
- 结构断言（无 return、注释占比）钉住「静态闸本该拦住的形状」，避免以后只改闸门
  却没留证据。

样本约定与新增流程见 `badcase/generation/README.md`。

不依赖 LLM / Judge0（force_local + 本地子进程），可进日常 `-m "not integration"`。
"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest

from code_tutor_agent.agents.agent_problem import verify_problem
from code_tutor_agent.generation.gateways.leetcode import LeetCodeGateway
from code_tutor_agent.generation.state import ProblemDraft
from code_tutor_agent.generation.verifier import CodeVerifier
from code_tutor_agent.guards.code_quality import code_quality_issues
from code_tutor_agent.sandbox.runner import run_solution

BAD_DIR = Path(__file__).resolve().parents[1] / "badcase" / "generation"
FIXTURES = sorted(BAD_DIR.glob("*.json"))
IDS = [p.stem for p in FIXTURES]

_TRUNCATED = "bc-gen-2026-11-18-02-truncated-cot.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _draft_of(fixture: dict) -> ProblemDraft:
    """把样本里的原始 problem 还原成 ProblemDraft（只取 CodeVerifier 用到的字段）。"""
    p = fixture["problem"]
    return ProblemDraft(
        topic=fixture["source"]["topic"],
        difficulty=p.get("difficulty") or fixture["source"]["difficulty_requested"],
        title=p.get("title") or "",
        description=p.get("description") or "",
        starter_code=p.get("starter_code") or "",
        optimal_solution=p.get("optimal_solution") or "",
        brute_solution=p.get("brute_solution") or "",
        examples=list(p.get("examples") or []),
        constraints=list(p.get("constraints") or []),
        function_signature=p.get("function_signature") or "",
    )


def _actual_of(status: str, detail: str) -> str:
    """与生成样本时同一套口径：WA 取 `got=` 之后，RE 取首行异常，其余取原始输出。"""
    d = (detail or "").strip()
    if status == "Wrong Answer":
        m = re.search(r"got=(.*)$", d, re.S)
        return m.group(1).strip() if m else d
    if status == "Runtime Error":
        return d.splitlines()[0].strip() if d else ""
    return d


def test_fixtures_present_and_unique():
    assert len(FIXTURES) == 3, [p.name for p in FIXTURES]
    ids = [_load(p)["id"] for p in FIXTURES]
    assert len(set(ids)) == 3
    assert all(i.startswith("BC-GEN-") for i in ids)


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_schema_complete(path: Path):
    d = _load(path)
    for key in (
        "schema_version", "id", "created_at", "source",
        "problem", "observed", "diagnosis", "regression",
    ):
        assert key in d, f"缺字段 {key}"
    assert d["schema_version"] == 1
    assert d["observed"]["stage"] == "examples"
    assert d["observed"]["verdict"] == "rejected"
    cross = d["observed"]["cross_check"]
    assert isinstance(cross["brute_present"], bool)
    assert cross["anchor_available"] in {"brute", "none"}
    assert d["regression"]["must_not_save"] is True
    # 分诊必须填：可修 / 部分可修 / 不可修，以及要不要优先重采样
    assert d["diagnosis"]["repairable"] in {"yes", "partial", "no"}
    assert isinstance(d["diagnosis"]["resample_preferred"], bool)
    assert d["diagnosis"]["defect_codes"]
    assert d["diagnosis"]["repair_strategy"]
    # 原始产物逐字段保留，供修复回路当上下文
    for field in ("title", "description", "examples", "constraints",
                  "optimal_solution", "starter_code", "function_signature"):
        assert field in d["problem"], f"problem 缺 {field}"


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_failure_is_reproducible(path: Path):
    """复跑全部示例，逐个复现登记时的 status / actual。"""
    d = _load(path)
    problem = d["problem"]
    gateway = LeetCodeGateway()
    tcs = gateway.parse_examples(
        problem.get("examples") or [], problem.get("starter_code") or ""
    )
    rows = d["observed"]["offline_full_scan"]
    assert len(tcs) == len(rows), f"示例解析数量漂移：{len(tcs)} != {len(rows)}"

    for tc, row in zip(tcs, rows):
        assert tc["input_args"] == row["input_args"], f"case{row['index']} 输入漂移"
        results = run_solution(
            problem.get("optimal_solution") or "",
            [tc],
            timeout=10.0,
            function_signature=problem.get("function_signature") or None,
            force_local=True,
        )
        assert results, f"case{row['index']} runner 无返回"
        r = results[0]
        assert r.status == row["status"], (
            f"case{row['index']} 状态漂移：{r.status} != {row['status']}；detail={r.detail[:200]}"
        )
        assert _actual_of(r.status, r.detail) == row["actual"], (
            f"case{row['index']} 实际输出漂移："
            f"{_actual_of(r.status, r.detail)!r} != {row['actual']!r}"
        )


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_cross_check_is_reproducible(path: Path):
    """双解交叉验证的证据也要锁住：谁对、谁错、有没有修复锚点。"""
    d = _load(path)
    problem = d["problem"]
    cross = d["observed"]["cross_check"]
    brute = (problem.get("brute_solution") or "").strip()
    assert bool(brute) is cross["brute_present"]
    if not brute:
        assert cross["brute_scan"] == []
        return

    gateway = LeetCodeGateway()
    tcs = gateway.parse_examples(
        problem.get("examples") or [], problem.get("starter_code") or ""
    )
    rows = cross["brute_scan"]
    assert len(tcs) == len(rows)
    for tc, row in zip(tcs, rows):
        results = run_solution(
            brute,
            [tc],
            timeout=10.0,
            function_signature=problem.get("function_signature") or None,
            force_local=True,
        )
        assert results, f"brute case{row['index']} runner 无返回"
        r = results[0]
        assert r.status == row["status"], (
            f"brute case{row['index']} 状态漂移：{r.status} != {row['status']}"
        )
        assert _actual_of(r.status, r.detail) == row["actual"]

    all_pass = all(r["status"] == "Passed" for r in rows)
    assert cross["anchor_available"] == ("brute" if all_pass else "none"), (
        "anchor_available 必须与 brute 是否全过一致"
    )


def test_truncated_sample_is_blocked_by_static_gate():
    """BC-GEN-...-02：静态闸必须拒绝，并同时报出「无 return」与「注释填充」。

    这道样本以前能过 verify_problem（有赋值/循环即算非桩），白跑一次沙箱；
    补闸后它应该在**进入 runner 之前**就被判废。
    """
    d = _load(BAD_DIR / _TRUNCATED)
    problem = d["problem"]
    code = problem["optimal_solution"]
    tree = ast.parse(code)

    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert funcs, "样本应包含函数定义"
    assert not any(
        isinstance(node, ast.Return)
        for f in funcs
        for node in ast.walk(f)
    ), "该样本的特征就是没有 return；若样本被改写请同步更新"

    lines = [ln for ln in code.splitlines() if ln.strip()]
    comment_ratio = sum(1 for ln in lines if ln.lstrip().startswith("#")) / len(lines)
    assert comment_ratio > 0.8, f"注释占比 {comment_ratio:.2f} 不符合截断样本特征"

    # 闸门本体：两条缺陷都要报出来，修复回路才有完整诊断
    issues = code_quality_issues(code, field="optimal_solution")
    joined = " | ".join(issues)
    assert "没有任何 return" in joined
    assert "注释占比" in joined

    # 两个入口都必须拒绝：generation 包入口 + LLM 出题第一道闸
    draft = _draft_of(d)
    ok, verifier_issues = CodeVerifier().verify(draft)
    assert not ok
    assert "没有任何 return" in " | ".join(verifier_issues)
    assert verify_problem(copy.deepcopy(problem)) is False


def test_triage_marks_the_unrepairable_one():
    """三个样本必须给出不同处置：可修 / 部分可修 / 不可修（应重采样）。"""
    triage = {_load(p)["id"]: _load(p)["diagnosis"] for p in FIXTURES}
    assert sum(1 for d in triage.values() if d["repairable"] == "yes") == 1
    assert sum(1 for d in triage.values() if d["repairable"] == "partial") == 1
    assert sum(1 for d in triage.values() if d["repairable"] == "no") == 1
    unrepairable = [d for d in triage.values() if d["repairable"] == "no"][0]
    assert unrepairable["resample_preferred"] is True
