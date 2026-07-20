"""针对 generator 路径 C（skill-engine import 主通道兜底）的单元测试。

覆盖 Phase 3 验收点：LLM 主通道（路径 B）全失败后，generator_node 走到
engine_adapter.generate_problem（进程内 import 通道，不再是 CLI 子进程），
返回合法``Command`` 并正确落库，端到端不抛异常；adapter 失败再降级静态池。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.nodes import generator
from code_tutor_agent.schemas.state import SessionState


def _make_state() -> SessionState:
    return SessionState(session_id="sid-pathc", topic="数组", difficulty="easy", mode="practice")


def _fake_adapter_problem() -> dict:
    return {
        "title": "Move Zeroes",
        "topic": "数组",
        "difficulty": "easy",
        "description": "将数组中所有 0 移动到末尾，保持非零元素相对顺序。",
        "starter_code": "class Solution:\n    def moveZeroes(self, nums): pass\n",
        "optimal_solution": "code",
        "test_cases": [
            {
                "input_args": ["[0,1,0,3,2]"],
                "expected_output": "[1,3,2,0,0]",
                "explanation": "基本用例",
            },
        ],
    }


def test_path_c_uses_adapter_when_llm_fails():
    """LLM 主通道失败 → 路径 C 走 engine_adapter，返回合法 Command，不抛异常。"""
    state = _make_state()
    fake_db = SimpleNamespace(starter_code="", optimal_solution="")

    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "generate_problem", side_effect=RuntimeError("LLM down")), \
         patch("code_tutor_agent.skills.engine_adapter.generate_problem",
               return_value=_fake_adapter_problem()) as mock_adapter, \
         patch.object(generator, "save_problem", return_value=7) as mock_save, \
         patch("code_tutor_agent.db.database.get_problem_by_id", return_value=fake_db), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    # 路径 C 确实走了 adapter（而非 CLI / 静态池）
    assert mock_adapter.called
    assert mock_adapter.call_args.args == ("数组", "easy")
    assert mock_adapter.call_args.kwargs.get("max_retries") == 1

    # 返回合法 Command，路由到 wait_for_submit_node
    assert cmd is not None
    assert cmd.goto == "wait_for_submit_node"

    # 落库内容来自 adapter 产物
    saved = mock_save.call_args[0][0]
    assert saved["title"] == "Move Zeroes"
    assert saved["test_cases"][0]["expected_output"] == "[1,3,2,0,0]"


def test_path_c_falls_back_to_static_pool_when_adapter_fails():
    """adapter 也失败 → 降级静态题库，仍返回合法 Command，不抛异常。"""
    state = _make_state()
    fake_db = SimpleNamespace(starter_code="", optimal_solution="")
    static_prob = {
        "title": "Two Pointers Basics",
        "topic": "双指针",
        "difficulty": "easy",
        "description": "静态题面",
        "starter_code": "",
        "optimal_solution": "",
        "test_cases": [
            {"input_args": ["[1,2]"], "expected_output": "[1,2]", "explanation": "s"},
        ],
    }

    with patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "generate_problem", side_effect=RuntimeError("LLM down")), \
         patch("code_tutor_agent.skills.engine_adapter.generate_problem",
               side_effect=RuntimeError("adapter boom")), \
         patch.object(generator, "get_static_problem",
               return_value=static_prob) as mock_static, \
         patch.object(generator, "save_problem", return_value=9) as mock_save, \
         patch("code_tutor_agent.db.database.get_problem_by_id", return_value=fake_db), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    # adapter 失败 → 走静态池
    assert mock_static.called
    assert cmd is not None
    assert cmd.goto == "wait_for_submit_node"
    saved = mock_save.call_args[0][0]
    assert saved["title"] == "Two Pointers Basics"
