"""Regression tests: AC 后允许「继续提交不同解法」。

背景：之前 AC 后 critic_node 直接 goto=__end__ 终止 graph，
导致 submit 端点因 status=="done" 拒绝任何重提交，
而前端又明确邀请「AC 了！可继续提交不同解法」。

修复：AC 后 critic_node 改为 goto=wait_for_submit_node（重新 interrupt 暂停），
submit 端点仅在「graph 真正终止（无待执行节点）」时才拒绝。
"""
from __future__ import annotations

from code_tutor_agent.nodes.critic import critic_node
from code_tutor_agent.schemas.state import (
    ProblemAttemptRecord,
    ProblemMeta,
    JudgeResult,
    SessionState,
    Submission,
)


def _base_state(**kw) -> SessionState:
    state = dict(
        session_id="s",
        problem=ProblemMeta(
            problem_id=7,
            title="Two Sum",
            topic="array",
            difficulty="easy",
            description="...",
            starter_code="",
            visible_test_cases=[],
        ),
        submissions=[
            Submission(
                index=1,
                code="x = 1",
                judge_results=[JudgeResult(status="AC", phase="base")],
            )
        ],
        last_verdict="AC",
        problem_history=[],
        total_problems=0,
        status="done",
        phase="reviewing",
    )
    state.update(kw)
    return SessionState(**state)


def test_ac_routes_to_wait_for_submit_node():
    """AC 后不再终止于 __end__，而是重新暂停在 wait_for_submit_node。"""
    cmd = critic_node(_base_state())
    assert cmd.goto == "wait_for_submit_node"
    assert cmd.update["phase"] == "reviewing"
    # 首次 AC：正常 flush 一题记录
    assert cmd.update["total_problems"] == 1
    assert len(cmd.update["problem_history"]) == 1


def test_ac_resubmit_same_verdict_dedup():
    """重复 AC 重提交：problem_history 不追加重复记录、total_problems 不 +1。"""
    existing = ProblemAttemptRecord(
        problem_id=7, title="Two Sum", difficulty="easy", verdict="AC"
    )
    cmd = critic_node(_base_state(problem_history=[existing]))
    assert cmd.goto == "wait_for_submit_node"
    assert "problem_history" not in cmd.update
    assert "total_problems" not in cmd.update


def test_ac_resubmit_diff_verdict_appends():
    """AC 后再次提交 WA：应作为新记录追加。"""
    existing = ProblemAttemptRecord(
        problem_id=7, title="Two Sum", difficulty="easy", verdict="AC"
    )
    cmd = critic_node(
        _base_state(problem_history=[existing], last_verdict="WA")
    )
    assert cmd.goto == "wait_for_submit_node"
    assert len(cmd.update["problem_history"]) == 2
    assert cmd.update["problem_history"][-1].verdict == "WA"
