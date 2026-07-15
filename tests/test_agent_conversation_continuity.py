"""回归测试：agent 模式整段对话连续性（docs/已知bug.md 2026-07-13）。

覆盖三个根因：
1. generator_node 出题时不再清空 tutor_messages（agent 模式保留出题前对话）。
2. next-problem 重入返回完整 tutor_messages（含做题中对话）。
3. 普通模式行为不变（每题仍以 welcome 开头）。

注：next-problem 重入依赖完整 graph + checkpointer，端到端验证；
这里用轻量单测覆盖 generator 的保留逻辑（普通/LC 两条分支同源）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_tutor_agent.schemas.state import Message as TutorMsg, SessionPhase


def _fake_leetcode_data() -> dict:
    return {
        "title": "Two Sum",
        "description": "Given an array...",
        "difficulty": "easy",
        "examples": [{"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"}],
        "starter_code": "class Solution:\n    def twoSum(self, nums, target):\n        pass",
        "tags": ["数组"],
        "hints": [],
    }


def _run_lc(existing_tutor_messages=None):
    from code_tutor_agent.nodes.generator import _generate_from_leetcode

    lc_data = _fake_leetcode_data()
    with patch("code_tutor_agent.db.database.save_problem", return_value=1), \
         patch("code_tutor_agent.nodes.generator._generate_optimal_for_leetcode_sync",
               return_value=None), \
         patch("code_tutor_agent.nodes.generator.writer", MagicMock(), create=True), \
         patch("code_tutor_agent.nodes.generator.get_stream_writer",
               return_value=MagicMock()):
        cmd = _generate_from_leetcode(
            "sid", lc_data,
            existing_tutor_messages=existing_tutor_messages,
        )
    return cmd


def test_lc_generator_agent_preserves_pre_gen_dialog():
    """agent 模式：出题前对话必须保留，并被追加 welcome。"""
    existing = [
        TutorMsg(role="tutor", content="我们来做一道数组题吧"),
        TutorMsg(role="user", content="好呀，简单点"),
    ]
    cmd = _run_lc(existing_tutor_messages=existing)
    msgs = cmd.update["tutor_messages"]

    # 第 1 条必须是原有出题前对话（未被清空）
    assert msgs[0].content == "我们来做一道数组题吧"
    # 原有 N 条 + 末尾 1 条 welcome
    assert len(msgs) == len(existing) + 1
    # welcome 在末尾，且包含题目标题
    assert msgs[-1].role == "tutor"
    assert "Two Sum" in msgs[-1].content
    assert cmd.update["phase"] == SessionPhase.solving


def test_lc_generator_normal_only_welcome():
    """普通模式（不传 existing）：仅 welcome，不清空问题（无历史可清）。"""
    cmd = _run_lc(existing_tutor_messages=None)
    msgs = cmd.update["tutor_messages"]
    assert len(msgs) == 1
    assert msgs[-1].role == "tutor"
    assert "Two Sum" in msgs[-1].content
