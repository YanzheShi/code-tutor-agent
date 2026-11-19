"""修复工具箱（P2）：5 个只读工具 + 会话级缓存 + 同步工具循环。

设计见 ``docs/generation-repair-agent-design.md`` §7。

边界
----
- 只读：跑代码、解析示例、静态检查；没有 save / 换题 / 降级。
- 单会话：缓存和配额挂在 ``ToolBox`` 实例上，绝不进模块级单例。
- 结果截断：最多 8 条用例、单条 detail 200 字、整包约 2KB。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from code_tutor_agent.generation.diagnosis import Diagnosis
from code_tutor_agent.generation.state import ProblemDraft

_OK_STATUSES = {"Passed", "Skipped"}
_MAX_CASES = 8
_MAX_ACTUAL_CHARS = 200
_MAX_RANDOM_INPUT_CHARS = 200
_TOOL_CODES = {
    "EXAMPLE_OUTPUT_MISMATCH",
    "REFERENCE_RE",
    "REFERENCE_TLE",
    "SOLVER_DISAGREEMENT",
    "SIGNATURE_MISMATCH",
    "EXAMPLE_PARSE_FAIL",
}


def toolbox_enabled() -> bool:
    import os

    raw = os.getenv("CTA_GEN_TOOLS_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def needs_tools(diagnosis: Diagnosis) -> bool:
    return bool(set(diagnosis.codes()) & _TOOL_CODES)


def _actual_of(result) -> str:
    detail = (getattr(result, "detail", "") or "").strip()
    status = getattr(result, "status", "")
    if status == "Wrong Answer":
        match = re.search(r"got=(.*)$", detail, re.S)
        detail = match.group(1).strip() if match else detail
    elif status == "Runtime Error":
        detail = detail.splitlines()[0].strip() if detail else ""
    return detail[:_MAX_ACTUAL_CHARS]


def _case_rows(cases: list[dict], results: list, *, with_stated: bool) -> list[dict]:
    rows = []
    for index, (case, result) in enumerate(zip(cases, results)):
        if index >= _MAX_CASES:
            break
        row = {
            "i": index,
            "status": getattr(result, "status", ""),
            "actual": _actual_of(result),
        }
        if with_stated:
            row["stated"] = str(case.get("expected_output", ""))[:_MAX_ACTUAL_CHARS]
        rows.append(row)
    return rows


class ToolBox:
    """一次修复会话的工具集合（只读 + 缓存 + 配额）。"""

    def __init__(
        self,
        agent,
        draft: ProblemDraft,
        diagnosis: Diagnosis,
        *,
        max_calls: int = 6,
        deadline: float = 0.0,
    ) -> None:
        self.agent = agent
        self.draft = draft
        self.diagnosis = diagnosis
        self.max_calls = max_calls
        self.deadline = deadline or (time.time() + 90.0)
        self.calls: list[dict] = []
        self._cache: dict[str, str] = {}
        self._used = 0

    # ── 公共 ──
    def tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                func=self.run_examples,
                name="run_examples",
                description="把候选代码跑在当前题目的全部示例上，返回每例 status/stated/actual。",
            ),
            StructuredTool.from_function(
                func=self.run_cases,
                name="run_cases",
                description="把候选代码跑在指定用例上（cases_json 是 JSON 数组，最多 8 条）。",
            ),
            StructuredTool.from_function(
                func=self.run_random,
                name="run_random",
                description="按函数签名与约束生成最多 6 组随机输入并运行候选代码，返回 actual。",
            ),
            StructuredTool.from_function(
                func=self.cross_check,
                name="cross_check",
                description="分别在示例上跑最优解与暴力解，返回两者是否一致及分歧用例。",
            ),
            StructuredTool.from_function(
                func=self.lint,
                name="lint",
                description="对候选代码做静态检查（结构 / 编译 / 注释 / CoT），返回缺陷列表。",
            ),
        ]

    # ── 工具实现 ──
    def run_examples(self, code: str) -> str:
        tool = "run_examples"
        cases = self._examples()
        if cases is None:
            return self._error(tool, "INVALID_ARGS", "当前题目的示例无法解析")
        return self._run(tool, code, cases, with_stated=True)

    def run_cases(self, code: str, cases_json: str) -> str:
        tool = "run_cases"
        try:
            raw = json.loads(cases_json)
        except Exception as exc:  # noqa: BLE001
            return self._error(tool, "INVALID_ARGS", f"cases_json 不是合法 JSON: {exc}")
        if not isinstance(raw, list) or not raw:
            return self._error(tool, "INVALID_ARGS", "cases_json 必须是非空数组")
        cases = []
        for item in raw[:_MAX_CASES]:
            if not isinstance(item, dict) or not isinstance(item.get("input_args"), list):
                return self._error(tool, "INVALID_ARGS", "每条用例必须含 input_args 数组")
            cases.append({
                "input_args": item["input_args"],
                "expected_output": str(item.get("expected_output", "")),
                "explanation": str(item.get("explanation", "repair probe")),
            })
        return self._run(tool, code, cases, with_stated=True)

    def run_random(self, code: str, n: int = 6) -> str:
        tool = "run_random"
        count = max(1, min(int(n or 6), 6))
        signature = self.draft.function_signature or ""
        if not signature:
            return self._error(tool, "INVALID_ARGS", "题目缺少 function_signature")
        try:
            inputs = self.agent.sandbox.random_inputs(
                signature,
                n=count,
                seed=7,
                constraints=self.draft.constraints or [],
                description=self.draft.description or "",
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(tool, "INVALID_ARGS", f"随机输入生成失败: {exc}")
        cases = []
        for args in inputs or []:
            text = "".join(str(a) for a in args)
            if len(text) > _MAX_RANDOM_INPUT_CHARS:
                continue
            cases.append({
                "input_args": args,
                "expected_output": "",
                "explanation": "repair random",
            })
            if len(cases) >= count:
                break
        if not cases:
            return self._error(tool, "INVALID_ARGS", "没有生成出规模合适的随机输入")
        return self._run(tool, code, cases, with_stated=False)

    def cross_check(self, optimal_code: str, brute_code: str) -> str:
        tool = "cross_check"
        cases = self._examples()
        if cases is None:
            return self._error(tool, "INVALID_ARGS", "当前题目的示例无法解析")
        started = time.perf_counter()
        key = self._key(tool, [optimal_code, brute_code, cases])
        if key in self._cache:
            self._note(tool, ok=True, summary="cache hit", cache_hit=True)
            return self._cache[key]
        if not self._take():
            return self._error(tool, "BUDGET_EXCEEDED", "工具调用次数已用完")

        optimal = self._batch(optimal_code, cases)
        brute = self._batch(brute_code, cases)
        agree = True
        rows = []
        for index, (opt, bru) in enumerate(zip(optimal, brute)):
            if index >= _MAX_CASES:
                break
            same = _actual_of(opt) == _actual_of(bru) and (
                getattr(opt, "status", "") in _OK_STATUSES
                and getattr(bru, "status", "") in _OK_STATUSES
            )
            agree = agree and same
            rows.append({
                "i": index,
                "optimal": _actual_of(opt),
                "brute": _actual_of(bru),
                "agree": same,
            })
        payload = self._ok(tool, {"agree": agree, "cases": rows}, started)
        self._cache[key] = payload
        self._note(tool, ok=True, summary=f"agree={agree}")
        return payload

    def lint(self, optimal_code: str, brute_code: str = "") -> str:
        from code_tutor_agent.guards.code_quality import (
            code_cot_issues,
            code_quality_issues,
        )

        tool = "lint"
        issues = code_quality_issues(optimal_code, field="optimal_solution")
        issues += code_cot_issues(optimal_code, field="optimal_solution")
        if brute_code.strip():
            issues += code_quality_issues(brute_code, field="brute_solution")
            issues += code_cot_issues(brute_code, field="brute_solution")
        self._note(tool, ok=True, summary=f"{len(issues)} issues")
        return json.dumps({"tool": tool, "ok": True, "issues": issues}, ensure_ascii=False)

    # ── 内部 ──
    def _examples(self) -> list[dict] | None:
        try:
            return self.agent.leetcode.parse_examples(
                self.draft.examples or [],
                self.draft.starter_code or self.draft.function_signature or "",
            )
        except Exception:  # noqa: BLE001
            return None

    def _batch(self, code: str, cases: list[dict]) -> list:
        signature = self.draft.function_signature or ""
        batch = getattr(self.agent.sandbox, "run_batch", None)
        if callable(batch):
            return batch(code, cases, function_signature=signature, timeout=5.0) or []
        results: list = []
        for case in cases:
            results.extend(
                self.agent.sandbox.run_solution(
                    code, [case], function_signature=signature,
                ) or []
            )
        return results

    def _run(self, tool: str, code: str, cases: list[dict], *, with_stated: bool) -> str:
        started = time.perf_counter()
        key = self._key(tool, [code, cases])
        if key in self._cache:
            self._note(tool, ok=True, summary="cache hit", cache_hit=True)
            return self._cache[key]
        if not self._take():
            return self._error(tool, "BUDGET_EXCEEDED", "工具调用次数已用完")
        try:
            results = self._batch(code, cases)
        except Exception as exc:  # noqa: BLE001
            return self._error(tool, "ENV_UNAVAILABLE", f"沙箱调用失败: {exc}")
        if not results or all(getattr(r, "status", "") == "Judge Error" for r in results):
            return self._error(tool, "ENV_UNAVAILABLE", "沙箱不可用")
        rows = _case_rows(cases, results, with_stated=with_stated)
        payload = self._ok(tool, {"cases": rows}, started)
        self._cache[key] = payload
        failed = sum(1 for r in rows if r["status"] not in _OK_STATUSES)
        self._note(tool, ok=True, summary=f"{len(rows)} cases, {failed} failed")
        return payload

    def _take(self) -> bool:
        if self._used >= self.max_calls:
            return False
        self._used += 1
        return True

    def _key(self, tool: str, payload: Any) -> str:
        raw = json.dumps([tool, payload], ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _note(self, tool: str, *, ok: bool, summary: str, cache_hit: bool = False) -> None:
        self.calls.append({
            "tool": tool,
            "ok": ok,
            "summary": summary,
            "cache_hit": cache_hit,
        })

    def _ok(self, tool: str, data: dict, started: float) -> str:
        payload = {
            "tool": tool,
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            **data,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, tool: str, kind: str, message: str) -> str:
        self._note(tool, ok=False, summary=f"{kind}: {message}"[:120])
        return json.dumps(
            {"tool": tool, "ok": False, "error_kind": kind, "message": message},
            ensure_ascii=False,
        )


def run_tool_loop_sync(
    llm,
    messages: list,
    tools: list[StructuredTool],
    *,
    deadline: float | None = None,
    max_rounds: int = 2,
) -> tuple[list, list[dict]]:
    """同步工具循环：让模型自己决定调用哪个只读工具。

    出题链路跑在 ``asyncio.to_thread`` 里，不能复用 async 的
    ``agents.tools.run_tool_loop``。返回 (messages, 工具调用摘要)。
    """
    from code_tutor_agent.llm_failover import invoke_with_failover

    allowed = {tool.name: tool for tool in tools}
    calls: list[dict] = []
    for _ in range(max_rounds):
        if deadline is not None and time.time() > deadline:
            break
        ai = invoke_with_failover(
            llm,
            lambda model: model.bind_tools(tools).invoke(messages),
            "problem",
        )
        messages.append(ai)
        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            name = call.get("name")
            tool = allowed.get(name)
            if tool is None:
                return messages, calls
            if deadline is not None and time.time() > deadline:
                messages.append(ToolMessage(
                    content=json.dumps(
                        {"tool": name, "ok": False, "error_kind": "BUDGET_EXCEEDED",
                         "message": "deadline reached"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=call["id"],
                ))
                break
            try:
                output = tool.invoke(call.get("args", {}))
            except Exception as exc:  # noqa: BLE001
                output = json.dumps(
                    {"tool": name, "ok": False, "error_kind": "INVALID_ARGS", "message": str(exc)},
                    ensure_ascii=False,
                )
            messages.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
            try:
                calls.append({"tool": name, "ok": json.loads(output).get("ok", False),
                              "summary": str(output)[:200]})
            except Exception:  # noqa: BLE001
                calls.append({"tool": name, "ok": False, "summary": str(output)[:200]})
    return messages, calls
