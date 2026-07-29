"""针对 generator_node 出题路径的单元测试。

验证「出题不依赖 skill-engine」后的行为：
* LLM 主通道（ProblemAgent）成功 → 走示例解析 + 参考解自验证，正常落库；
* LLM 全失败 → ProblemAgent 回退静态题库 → generator_node 采用静态题落库；
* 静态题库也为空（极端）→ generator_node 强制再拉一次静态兜底，仍返回合法 Command。

覆盖 Phase 改造点：移除原路径 C（engine_adapter / skill-engine），统一走
ProblemAgent（原生 LLM + 静态兜底）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.nodes import generator
from code_tutor_agent.agents.agent_problem import ProblemChannel
from code_tutor_agent.schemas.state import SessionState


def _make_state() -> SessionState:
    return SessionState(session_id="sid-pathc", topic="数组", difficulty="easy", mode="practice")


def _fake_static_problem() -> dict:
    return {
        "title": "Two Pointers Basics",
        "topic": "双指针",
        "difficulty": "easy",
        "description": "静态题面",
        "starter_code": "class Solution:\n    def f(self, a, b): pass\n",
        "optimal_solution": "class Solution:\n    def f(self, a, b): return a\n",
        "test_cases": [
            {"input_args": ["[1,2]"], "expected_output": "[1,2]", "explanation": "s"},
        ],
    }


def _fake_outcome(channel: str, title: str):
    """构造一个轻量 GenerationOutcome：problem.model_dump() 返回给定 dict。"""
    static = _fake_static_problem()
    static["title"] = title
    fake_problem = SimpleNamespace(model_dump=lambda: dict(static))
    return SimpleNamespace(ok=True, channel=channel, problem=fake_problem)


def test_generator_uses_problem_agent_static_fallback():
    """LLM 主通道失败 → ProblemAgent 回退静态题库 → generator_node 采用并落库。"""
    state = _make_state()
    fake_db = SimpleNamespace(starter_code="", optimal_solution="")

    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(
             generator, "ProblemAgent",
             return_value=SimpleNamespace(generate=lambda: _fake_outcome(ProblemChannel.STATIC, "Two Pointers Basics")),
         ), \
         patch.object(generator, "save_problem", return_value=9) as mock_save, \
         patch("code_tutor_agent.db.database.get_problem_by_id", return_value=fake_db), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    assert cmd is not None
    assert cmd.goto == "wait_for_submit_node"
    saved = mock_save.call_args[0][0]
    assert saved["title"] == "Two Pointers Basics"


def test_generator_forces_static_when_problem_agent_returns_nothing():
    """ProblemAgent 全失败（含静态为空）→ generator_node 强制再拉静态兜底，不抛异常。"""
    state = _make_state()
    fake_db = SimpleNamespace(starter_code="", optimal_solution="")

    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(
             generator, "ProblemAgent",
             return_value=SimpleNamespace(generate=lambda: SimpleNamespace(ok=False, channel=ProblemChannel.STATIC, problem=None)),
         ), \
         patch.object(generator, "get_static_problem", return_value=_fake_static_problem()) as mock_static, \
         patch.object(generator, "save_problem", return_value=11) as mock_save, \
         patch("code_tutor_agent.db.database.get_problem_by_id", return_value=fake_db), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    assert mock_static.called
    assert cmd is not None
    assert cmd.goto == "wait_for_submit_node"
    saved = mock_save.call_args[0][0]
    assert saved["title"] == "Two Pointers Basics"
