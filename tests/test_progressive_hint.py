"""渐进式辅导护栏 + 提交归属修复的回归测试（2026-09-06）。

覆盖两批修复：
1. 空壳代码（pass/.../模板）提交绝不触发 LLM 写「修复建议」——否则导师会把
   整题答案全文倒出（用户实锤 badcase：误提交 starter 模板后收到完整参考解）。
2. save_submission 漏传 user_id 导致个人中心/提交记录永远为空（查询端按
   真实用户过滤、落库全是 'default'）——修复 + 存量回填的幂等回归。
"""

from __future__ import annotations

import sqlite3

import pytest

from code_tutor_agent.agents.agent_judge import (
    _deterministic_verdict,
    _stub_feedback,
    analyze_judge_results,
    is_stub_solution,
)
from code_tutor_agent.db import database as db
from code_tutor_agent.sandbox.runner import RunnerResult


# ──────────────────────────────────────────────
#  is_stub_solution：空壳代码判定（ast，确定性）
# ──────────────────────────────────────────────


class TestIsStubSolution:
    def test_starter_template_with_pass(self):
        """用户实锤 badcase：直接提交 starter 模板（方法体 pass）→ 空壳。"""
        code = (
            "# Definition for singly-linked list.\n"
            "class ListNode:\n"
            "    def __init__(self, val=0, next=None):\n"
            "        self.val = val\n"
            "        self.next = next\n"
            "\n"
            "class Solution:\n"
            "    def reorderPlaylist(self, playlist: Optional[ListNode]) -> Optional[ListNode]:\n"
            "        pass\n"
        )
        assert is_stub_solution(code) is True

    def test_ellipsis_body(self):
        assert is_stub_solution("class Solution:\n    def f(self):\n        ...\n") is True

    def test_docstring_only_body(self):
        code = 'class Solution:\n    def f(self):\n        """todo"""\n'
        assert is_stub_solution(code) is True

    def test_return_none_only(self):
        assert is_stub_solution("class Solution:\n    def f(self):\n        return None\n") is True

    def test_empty_code(self):
        assert is_stub_solution("") is True
        assert is_stub_solution("   \n  ") is True

    def test_real_logic_is_not_stub(self):
        code = (
            "class Solution:\n"
            "    def twoSum(self, nums, target):\n"
            "        seen = {}\n"
            "        for i, x in enumerate(nums):\n"
            "            if target - x in seen:\n"
            "                return [seen[target - x], i]\n"
            "            seen[x] = i\n"
            "        return []\n"
        )
        assert is_stub_solution(code) is False

    def test_partial_logic_is_not_stub(self):
        """写了一半（有真实语句 + 一个 return 常量结尾）不算空壳——写过了就走正常反馈。"""
        code = (
            "class Solution:\n"
            "    def f(self, nums):\n"
            "        total = 0\n"
            "        for x in nums:\n"
            "            total += x\n"
            "        return total\n"
        )
        assert is_stub_solution(code) is False

    def test_syntax_error_is_not_stub(self):
        """语法错误 = 写了代码但有 CE，应走正常反馈（能看到具体错误），不算空壳。"""
        assert is_stub_solution("class Solution:\n    def f(self):\n        x = = 1\n") is False

    def test_no_function_is_not_stub(self):
        """完全没函数定义的乱码交给正常反馈处理，不按空壳论。"""
        assert is_stub_solution("print('hello')") is False


# ──────────────────────────────────────────────
#  空壳闸门：analyze_judge_results 不进 LLM、不泄露答案
# ──────────────────────────────────────────────


def _wa_results(n: int = 2) -> list:
    return [
        RunnerResult(
            test_case_id=i, status="Wrong Answer", detail="",
            runtime_ms=1.0, memory_kb=100.0,
            input_args=[ "[1,2,3]" ], expected_output="6", actual_output="None",
        )
        for i in range(n)
    ]


class TestStubGateInAnalyze:
    def test_stub_submission_skips_llm_and_returns_no_spoiler(self, monkeypatch):
        """空壳提交：LLM 绝不能被调用；反馈是确定性的鼓励文案，不含代码。"""
        def _boom(*a, **k):
            raise AssertionError("LLM must not be called for stub submissions")
        monkeypatch.setattr("code_tutor_agent.agents.agent_judge.get_llm", _boom)

        analysis = analyze_judge_results(
            code="class Solution:\n    def f(self):\n        pass\n",
            title="重排播放列表", difficulty="medium", topic="链表",
            description="把链表重排…", results=_wa_results(), forced_verdict="WA",
        )
        assert analysis.verdict == "WA"
        assert analysis.should_retry is True
        # 不泄露任何解题代码 / 步骤
        assert "class Solution" not in analysis.repair_suggestion
        assert "def " not in analysis.repair_suggestion
        assert "快慢指针" not in analysis.repair_suggestion  # 不给技巧级提示也至少不倒答案
        assert len(analysis.warm_feedback) > 0

    def test_non_stub_goes_to_llm_path(self, monkeypatch):
        """真实代码：走 LLM 路径（这里以 get_llm 抛错验证进入了 LLM 分支并吃掉异常降级）。"""
        def _boom(*a, **k):
            raise RuntimeError("llm down")
        monkeypatch.setattr("code_tutor_agent.agents.agent_judge.get_llm", _boom)

        analysis = analyze_judge_results(
            code="class Solution:\n    def f(self, nums):\n        return sorted(nums)\n",
            title="排序", difficulty="easy", topic="排序",
            description="排序", results=_wa_results(), forced_verdict="WA",
        )
        # LLM 挂了 → 确定性降级反馈，verdict 仍以执行引擎为准
        assert analysis.verdict == "WA"
        assert analysis.should_retry is True

    def test_stub_but_ac_still_normal_path(self, monkeypatch):
        """空壳但判成 AC（理论罕见，如题目本就要求返回 None）：不误杀，走正常路径。"""
        class _FakeStructured:
            def invoke(self, msgs):
                from code_tutor_agent.agents.agent_judge import JudgeAnalysis
                return JudgeAnalysis(verdict="AC", warm_feedback="恭喜！", repair_suggestion="", should_retry=False)

        class _FakeLLM:
            def with_structured_output(self, schema):
                return _FakeStructured()

        monkeypatch.setattr(
            "code_tutor_agent.agents.agent_judge.get_llm", lambda *a, **k: _FakeLLM(),
        )
        results = [
            RunnerResult(test_case_id=0, status="Passed", detail="", runtime_ms=1.0,
                         memory_kb=1.0, input_args=["[]"], expected_output="", actual_output=""),
        ]
        analysis = analyze_judge_results(
            code="class Solution:\n    def f(self):\n        pass\n",
            title="t", difficulty="easy", topic="链表", description="d",
            results=results, forced_verdict="AC",
        )
        assert analysis.verdict == "AC"

    def test_stub_feedback_content_shape(self):
        fb = _stub_feedback("WA", "链表")
        assert fb.verdict == "WA" and fb.should_retry is True
        assert "方法体" in fb.warm_feedback  # 点明方法体为空
        assert "class" not in fb.repair_suggestion and "def " not in fb.repair_suggestion


# ──────────────────────────────────────────────
#  提交归属：save_submission user_id + 存量回填
# ──────────────────────────────────────────────


@pytest.fixture
def tmp_db():
    """初始化表结构（PG 版：不再 monkeypatch DB_PATH/_WAL_READY——SQLite 已下线，
    隔离由 conftest 的 _pg_clean_tables 每测试后清表保证，镜像 test_db_concurrency 模式）。"""
    db.init_db()


def _mk_problem(suffix: str) -> int:
    """PG 强制 submissions→problems 外键（SQLite 时代 FK 默认不生效），先建题再存提交。"""
    pid, _ = db.save_problem({
        "title": f"测试题{suffix}",
        "topic": "数组",
        "difficulty": "easy",
        "description": "测试",
        "test_cases": [],
        "starter_code": f"class Solution:\n    def solve{suffix}(self):\n        pass",
    })
    return pid


class TestSubmissionUserIsolation:
    def test_save_and_query_by_user(self, tmp_db):
        """落库带 user_id → 按用户过滤查询可见；他人查不到。"""
        pid = _mk_problem("FkA")
        db.save_submission(pid, "code", "AC", [], session_id="s1", user_id="7")
        db.save_submission(pid, "code", "WA", [], session_id="s2", user_id="8")

        mine = db.get_submissions_by_problem(pid, user_id="7")
        assert len(mine) == 1
        others = db.get_submissions_by_problem(pid, user_id="8")
        assert len(others) == 1
        assert db.get_submissions_by_problem(pid, user_id="9") == []

        allsubs = db.get_all_submissions(user_id="7")
        assert len(allsubs) == 1

    def test_backfill_reowns_legacy_default_rows(self, tmp_db):
        """存量 'default' 行 + 会话归属已知 → 启动回填后按真实用户可见。"""
        pid1, pid2 = _mk_problem("FkB"), _mk_problem("FkC")
        # 会话 s9 属于用户 7；s10 无归属记录
        db.touch_session("s9", "7")
        db.save_submission(pid1, "legacy", "WA", [], session_id="s9", user_id="default")
        db.save_submission(pid2, "orphan", "WA", [], session_id="s10", user_id="default")

        n = db.backfill_submissions_user_id()
        assert n == 1  # 只回填有归属的 1 行，无归属的不臆测

        mine = db.get_all_submissions(user_id="7")
        assert len(mine) == 1
        # 无归属会话的提交保持 default，不丢不串
        assert db.get_all_submissions(user_id="default") is not None
        assert len(db.get_submissions_by_problem(pid2, user_id="7")) == 0

    def test_backfill_idempotent(self, tmp_db):
        pid = _mk_problem("FkD")
        db.touch_session("s1", "3")
        db.save_submission(pid, "c", "AC", [], session_id="s1", user_id="default")
        assert db.backfill_submissions_user_id() == 1
        assert db.backfill_submissions_user_id() == 0  # 第二次无行可动


# ──────────────────────────────────────────────
#  chat 状态说明：空壳强化纪律
# ──────────────────────────────────────────────


class TestStateNoteStub:
    def test_stub_note_forbids_solution(self):
        from code_tutor_agent.api.routers.chat import _build_state_note
        note = _build_state_note("solving", "WA", code_stub=True)
        assert "空壳" in note and "绝对禁止" in note

    def test_normal_note_unchanged(self):
        from code_tutor_agent.api.routers.chat import _build_state_note
        note = _build_state_note("solving", "", code_stub=False)
        assert "空壳" not in note
        assert "不要直接给出完整代码" in note
