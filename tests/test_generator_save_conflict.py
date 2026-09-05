"""回归：LLM 出题落库撞 title UNIQUE → 换题重采样（2026-09-04 线上复现）。

背景：LLM 随机出题《重排链表》与库中已有题(id=2, 早期种子)撞 ``problems.title``
UNIQUE 约束，而 save 只在 starter_code 归一化 / source_url 维度去重、无 title 兜底，
于是 INSERT 抛 IntegrityError → ProblemGenerationAgent 整轮失败 → generator_node
把会话打回 dialog → 客户端 wait_ready 空转 240s 超时（症状酷似死锁）。

修复：把「落库冲突」归入 LLM 通道外层重试语义——丢弃本 draft 换题重采样，
最多 max_retries 次；耗尽后 draft=None 自然落入降级链。URL 导入 / 降级链产物的
落库失败仍直接报错（不静默换题），语义不变。

验证目标：
* LLM 首次出题撞库 → 自动重采样第二题 → ok=True，落库的是第二题；
* 不再出现「落库失败整轮报错」把会话卡死在 dialog 的行为。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import sqlite3  # noqa: E402

from code_tutor_agent.generation import ProblemGenerationAgent  # noqa: E402
from code_tutor_agent.generation.state import (  # noqa: E402
    GenerationContext,
    ProblemDraft,
)


def _draft(title: str) -> ProblemDraft:
    return ProblemDraft(
        topic="链表", difficulty="medium", title=title,
        description=f"{title} 题面",
        starter_code="class Solution:\n    def f(self, head): pass\n",
        optimal_solution="class Solution:\n    def f(self, head): return head\n",
        function_signature="head: ListNode -> ListNode",
        test_cases=[{"input_args": ["[1,2,3]"], "expected_output": "[1,2,3]", "explanation": "s"}],
    )


def _always_pass_verifier():
    """verify(draft) → (True, [])，让自洽校验恒通过（聚焦落库冲突行为）。"""
    return SimpleNamespace(verify=lambda draft: (True, []))


class _ConflictThenOkStore:
    """前 fail_n 次 save 抛 title UNIQUE 冲突，之后正常落库并记录。"""

    def __init__(self, fail_n: int = 1):
        self.fail_n = fail_n
        self.calls = 0
        self.saved: list[ProblemDraft] = []

    def save(self, draft: ProblemDraft) -> tuple[int, bool]:
        self.calls += 1
        if self.calls <= self.fail_n:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: problems.title")
        self.saved.append(draft)
        return 100 + self.calls, False

    def unac_problem(self, *a, **k):
        return None

    def static_problem(self, *a, **k):
        return None


def _make_agent(store, titles) -> ProblemGenerationAgent:
    """llm.generate_problem 按 titles 依次出题；PULL/HISTORY/STATIC 均无。"""
    drafts = [_draft(t) for t in titles]
    llm = SimpleNamespace(
        generate_problem=lambda *a, **k: drafts[min(len(drafts) - 1, store.calls)] if drafts else None,
        generate_optimal=lambda *a, **k: "",
        generate_dual=lambda *a, **k: None,
    )
    # _llm_generate 内部还要走「示例解析 + 参考解自验证」：parse_examples 需返回
    # 用例，run_solution 需返回 Passed 结果才能回填 expected_output（否则返回 None）。
    leetcode = SimpleNamespace(
        list=lambda *a, **k: [],
        to_lc_dict=lambda p: None,
        parse_examples=lambda examples, starter: [
            {"input_args": ["[1,2,3]"], "expected_output": "", "explanation": "s"},
        ],
        extract_signature=lambda s: "",
    )
    sandbox = SimpleNamespace(
        struct_prologue=lambda *a, **k: "",
        compile=lambda code: True,
        run_solution=lambda *a, **k: [SimpleNamespace(status="Passed", detail="[1,2,3]")],
        random_inputs=lambda *a, **k: [],
        sanitize=lambda *a, **k: None,
        needs_sorted_inputs=lambda *a, **k: False,
    )
    return ProblemGenerationAgent(
        leetcode=leetcode, llm=llm, store=store, sandbox=sandbox,
        verifier=_always_pass_verifier(),
    )


def test_llm_title_collision_triggers_resample():
    """LLM 首题撞 title → 自动换题重采样，第二题正常落库 → ok=True。"""
    store = _ConflictThenOkStore(fail_n=1)
    agent = _make_agent(store, titles=["重排链表", "合并 K 个升序链表"])

    ctx = GenerationContext(topic="链表", difficulty="medium")
    with patch("code_tutor_agent.generation.problem_generation_agent.MAX_RETRIES", 3):
        result = agent.run(ctx)

    assert result.ok, f"应重采样成功而非整轮失败: {result.error}"
    assert result.channel == "llm"
    assert store.calls == 2                       # 撞一次 + 重采样一次
    assert len(store.saved) == 1                  # 只有第二题真正落库
    assert store.saved[0].title == "合并 K 个升序链表"
    assert result.draft.title == "合并 K 个升序链表"


def test_llm_persistent_save_failure_falls_back():
    """每次 save 都撞库（重采样耗尽）→ 不落任何题，走降级链/报错，绝不静默塞题。"""
    store = _ConflictThenOkStore(fail_n=99)
    agent = _make_agent(store, titles=["重排链表", "合并 K 个升序链表", "环形链表"])

    ctx = GenerationContext(topic="链表", difficulty="medium")
    with patch("code_tutor_agent.generation.problem_generation_agent.MAX_RETRIES", 3):
        result = agent.run(ctx)

    # LLM 三次全部撞库 → draft=None → 降级链全空 → ok=False（不卡死不误报成功）
    assert result.ok is False
    assert store.saved == []                      # 无任何题落库
