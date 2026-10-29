"""Tests for the /submit 拆链路（2026-10-27）：判题先回、辅导文案后台补写。

覆盖四件事：
  1. 标记的传递：resume 载荷 → ``state.tutor_deferred``（sample 一律不接受）。
  2. 图内短路：辅导节点与 AC 线在 deferred 时**绝不**调 LLM —— 这才是把分钟级
     等待从请求路径摘掉的地方。
  3. 后台补写：文案算完写回状态，供前端轮询 ``warm_feedback`` 取。
  4. 陈旧写入防护：生成期间用户又提交一次或换了题，必须丢弃而不是盖到新一轮上。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from code_tutor_agent.db.models import DBProblem

from code_tutor_agent.agents.agent_judge import JudgeAnalysis
from code_tutor_agent.api.services import tutor_feedback as tf
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.nodes.agent_tutor import agent_tutor_node
from code_tutor_agent.nodes.wait_for_submit import wait_for_submit_node
from code_tutor_agent.sandbox.runner import RunnerResult
from code_tutor_agent.schemas.state import ProblemMeta, SessionState, Submission

_EXEC_RESULTS = [
    {
        "test_case_id": 0,
        "status": "Wrong Answer",
        "detail": "expected=[0,1] got=[0,0]",
        "runtime_ms": 5.0,
        "memory_kb": 1024.0,
        "expected_output": "[0,1]",
        "actual_output": "[0,0]",
    },
]

_AC_DB_PROBLEM = DBProblem(
    id=1, title="测试题", topic="数组", difficulty="easy", description="测试描述",
    test_cases_json='[{"input_args": ["[1,2]", "3"], "expected_output": "[0,1]"}]',
)


def _tutor_state(deferred: bool, scope: str = "full") -> SessionState:
    return SessionState(
        session_id="t-defer",
        mode="agent",
        status="tutoring",
        last_verdict="WA",
        judge_cycle=3,
        judge_scope=scope,
        tutor_deferred=deferred,
        topic="数组",
        difficulty="easy",
        problem=ProblemMeta(
            problem_id=1, title="测试题", topic="数组",
            difficulty="easy", description="测试描述",
        ),
        submissions=[Submission(index=1, code="class Solution:\n    pass", timestamp="")],
        last_exec_results=_EXEC_RESULTS,
    )


def _judge_state(deferred: bool, scope: str = "full") -> SessionState:
    return SessionState(
        session_id="t-judge-ac",
        mode="agent",
        status="awaiting_submit",
        judge_scope=scope,
        tutor_deferred=deferred,
        problem=ProblemMeta(
            problem_id=1, title="测试题", topic="数组", difficulty="easy",
            description="测试", starter_code="class Solution:\n    pass",
        ),
        submissions=[Submission(index=1, code="class Solution:\n    pass", timestamp="")],
    )


def _resume(payload: dict) -> dict:
    """让 interrupt() 直接返回给定的 resume 载荷。"""
    with patch("code_tutor_agent.nodes.wait_for_submit.interrupt", return_value=payload):
        return wait_for_submit_node(SessionState(session_id="t-resume", mode="agent"))


class TestDeferFlagPlumbing:
    def test_full_submit_carries_flag(self):
        out = _resume({"code": "print(1)", "scope": "full", "defer_tutor": True})
        assert out["judge_scope"] == "full"
        assert out["tutor_deferred"] is True

    def test_sample_run_never_defers(self):
        """/run 本来就不调 LLM，即使误带 defer_tutor 也不能置位。"""
        out = _resume({"code": "print(1)", "scope": "sample", "defer_tutor": True})
        assert out["tutor_deferred"] is False

    def test_plain_resume_resets_flag(self):
        """不显式写 False 会让上一轮的延迟标记残留（通道 last-wins），
        之后每一轮都只出判题结果、再也等不到点评。"""
        out = _resume({"code": "print(1)"})
        assert out["tutor_deferred"] is False


class TestTutorNodeShortCircuit:
    def test_deferred_skips_llm_entirely(self):
        state = _tutor_state(deferred=True)
        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            result = agent_tutor_node(state)
            mock_analyze.assert_not_called()

        assert result["status"] == "awaiting_submit"
        # 清空陈旧文案：前端在文案到达前不该看到上一轮的点评
        assert result["warm_feedback"] == ""
        assert result["repair_suggestion"] == ""
        assert "tutor_messages" not in result

    def test_not_deferred_still_generates(self):
        state = _tutor_state(deferred=False)
        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            mock_analyze.return_value = JudgeAnalysis(
                verdict="WA", warm_feedback="差一点",
                repair_suggestion="看边界", should_retry=True,
            )
            result = agent_tutor_node(state)
        assert result["warm_feedback"] == "差一点"

    def test_sample_scope_immune_to_stale_flag(self):
        """运行线走确定性文案，不受延迟标记影响。"""
        state = _tutor_state(deferred=True, scope="sample")
        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as mock_analyze:
            result = agent_tutor_node(state)
            mock_analyze.assert_not_called()
        assert "样例用例" in result["warm_feedback"]


class TestJudgeACDeferral:
    """AC 线的祝贺/复杂度分析同样移出请求（router 不经辅导节点，靠判题节点自己短路）。"""

    def _mocks(self, mock_db, mock_run, mock_analyze):
        mock_db.return_value = _AC_DB_PROBLEM
        mock_run.return_value = [RunnerResult(0, "Passed", "[0,1]", runtime_ms=5.0)]
        mock_analyze.return_value = JudgeAnalysis(
            verdict="AC", warm_feedback="恭喜！全部通过 🎉",
            repair_suggestion="", should_retry=False,
        )

    def test_ac_deferred_does_not_call_llm(self):
        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
            patch("code_tutor_agent.nodes.agent_judge._apply_side_effects"),
        ):
            self._mocks(mock_db, mock_run, mock_analyze)
            result = agent_judge_node(_judge_state(deferred=True))
            mock_analyze.assert_not_called()

        assert result["last_verdict"] == "AC"
        # 交棒载荷照写：后台段靠它生成文案，不重跑用例
        assert result["last_exec_results"]
        assert result["warm_feedback"] == ""
        assert "tutor_messages" not in result

    def test_ac_immediate_path_unchanged_when_not_deferred(self):
        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
            patch("code_tutor_agent.nodes.agent_judge._apply_side_effects"),
        ):
            self._mocks(mock_db, mock_run, mock_analyze)
            result = agent_judge_node(_judge_state(deferred=False))
            mock_analyze.assert_called_once()

        assert result["warm_feedback"] == "恭喜！全部通过 🎉"

    def test_sample_ac_immune_to_stale_flag(self):
        """运行（sample）的 AC 反馈是本地拼的、不花钱，标记残留也不该把它延迟掉。"""
        with (
            patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id") as mock_db,
            patch("code_tutor_agent.nodes.agent_judge.run_solution") as mock_run,
            patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as mock_analyze,
            patch("code_tutor_agent.nodes.agent_judge._apply_side_effects"),
        ):
            self._mocks(mock_db, mock_run, mock_analyze)
            result = agent_judge_node(_judge_state(True, scope="sample"))

        mock_analyze.assert_not_called()
        assert "样例用例" in result["warm_feedback"]


class _FakeGraph:
    """只实现 get_state，足以驱动后台补写逻辑（不连 DB、不跑真图）。"""

    def __init__(self, values: dict):
        self.values = values

    def get_state(self, config):
        return SimpleNamespace(values=self.values)

    def advance(self, **changes):
        self.values.update(changes)


_CONFIG = {"configurable": {"thread_id": "t-bg"}}


def _values(**over) -> dict:
    base = {
        "session_id": "t-bg", "mode": "agent", "judge_cycle": 3, "judge_scope": "full",
        "tutor_deferred": True, "last_verdict": "WA", "status": "awaiting_submit",
        "problem": {"problem_id": 1, "title": "测试题", "topic": "数组",
                    "difficulty": "easy", "description": "测试描述"},
        "submissions": [{"index": 1, "code": "print(1)", "timestamp": ""}],
        "last_exec_results": _EXEC_RESULTS,
        "tutor_messages": [],
        "topic": "数组", "difficulty": "easy",
    }
    base.update(over)
    return base


class TestDeferredWriteBack:
    def test_writes_only_the_copy(self):
        graph = _FakeGraph(_values())
        with patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write:
            tf._generate_and_write(graph, _CONFIG, 3, 1)

            # 只补文案：status/phase 由判题节点与 critic 定好，写回动它会踩到前端 done 判定
            written = mock_write.call_args.args[2]
            assert set(written) == {"warm_feedback", "repair_suggestion", "tutor_messages"}
            assert written["tutor_messages"][-1]["content"]

    def test_drops_when_user_already_resubmitted(self):
        graph = _FakeGraph(_values(judge_cycle=4))
        with patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write:
            tf._generate_and_write(graph, _CONFIG, 3, 1)
            mock_write.assert_not_called()

    def test_drops_when_problem_swapped_after_ac(self):
        """AC 后点「下一题」：critic 的 ABANDON 分支会清题并清空 tutor_messages，
        迟到的点评会把上一题的讲解贴到新题下面。"""
        graph = _FakeGraph(_values(problem=None))
        with patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write:
            tf._generate_and_write(graph, _CONFIG, 3, 1)
            mock_write.assert_not_called()

    def test_drops_when_flag_cleared(self):
        graph = _FakeGraph(_values(tutor_deferred=False))
        with patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write:
            tf._generate_and_write(graph, _CONFIG, 3, 1)
            mock_write.assert_not_called()

    def test_rechecks_after_generation_before_writing(self):
        """LLM 那一下可能耗时数十秒，期间用户又提交了一轮 —— 落库前必须再校验一次。"""
        graph = _FakeGraph(_values())

        def _build_then_advance(state):
            graph.advance(judge_cycle=4)
            return {"warm_feedback": "迟到的点评", "repair_suggestion": "", "tutor_messages": []}

        with (
            patch("code_tutor_agent.nodes.agent_tutor.build_tutor_update",
                  side_effect=_build_then_advance),
            patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write,
        ):
            tf._generate_and_write(graph, _CONFIG, 3, 1)
            mock_write.assert_not_called()


class TestScheduler:
    def test_no_running_loop_returns_none(self):
        """同步上下文里调度不了后台任务，只能安静跳过（本轮仅缺点评）。"""
        assert tf.schedule_tutor_feedback(
            _FakeGraph(_values()), _CONFIG, expected_cycle=3, expected_problem_id=1
        ) is None

    @pytest.mark.asyncio
    async def test_runs_write_back_off_the_event_loop(self):
        graph = _FakeGraph(_values())
        with patch("code_tutor_agent.api.deps.pause_safe_update") as mock_write:
            task = tf.schedule_tutor_feedback(
                graph, _CONFIG, expected_cycle=3, expected_problem_id=1
            )
            assert task is not None
            await task
        mock_write.assert_called_once()
