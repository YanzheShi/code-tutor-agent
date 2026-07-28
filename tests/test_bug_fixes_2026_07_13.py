"""2026-07-13 bug 修复回归测试（Bug 5/6/7/8/9 相关，不依赖 LLM/DB）。

运行:
    uv run pytest tests/test_bug_fixes_2026_07_13.py -q
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_tutor_agent.agents.problem_generator import generate_problem
from code_tutor_agent.schemas.api import NextProblemResp


class _FailingStructured:
    """模拟 with_structured_output 返回的对象：invoke 始终失败。"""

    def __call__(self, *args, **kwargs):
        raise Exception("truncated at 16384 tokens")

    def invoke(self, *args, **kwargs):
        raise Exception("truncated at 16384 tokens")

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self


def test_generate_problem_all_llm_failures_raises_runtimeerror():
    """Bug7: 全部 LLM 调用失败时不应 UnboundLocalError，而应抛 RuntimeError 由上层降级。"""
    failing_llm = MagicMock()
    failing_llm.with_structured_output.return_value = _FailingStructured()
    with patch(
        "code_tutor_agent.agents.agent_problem.get_llm", return_value=failing_llm
    ):
        with pytest.raises(RuntimeError):
            generate_problem("数组", "easy", max_retries=0)


def test_generate_problem_passes_correct_purpose():
    """generate_problem 应传递正确的 purpose 给 get_llm。"""
    captured: dict = {}
    failing_llm = MagicMock()
    failing_llm.with_structured_output.return_value = _FailingStructured()

    def _get_llm(*_a, **kw):
        captured.update(kw)
        return failing_llm

    with patch(
        "code_tutor_agent.agents.agent_problem.get_llm", side_effect=_get_llm
    ):
        with pytest.raises(RuntimeError):
            generate_problem("数组", "easy", max_retries=0)
    assert captured.get("purpose") == "problem"


def test_next_problem_resp_allows_null_problem_with_history():
    """Bug5/8/9: agent 重入对话时 problem 可为 None，并携带历史 tutor_messages。"""
    resp = NextProblemResp(
        session_id="s1",
        problem=None,
        phase="dialog",
        tutor_messages=[
            {"role": "tutor", "content": "之前的对话"},
            {"role": "user", "content": "想练双指针"},
        ],
    )
    assert resp.problem is None
    assert resp.phase == "dialog"
    assert len(resp.tutor_messages) == 2
