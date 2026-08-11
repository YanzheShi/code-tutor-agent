"""针对 generator_node 出题路径的单元测试。

生成子 Agent 架构（docs/generation-subagent-design.md §2/§12）下，generator_node
是薄壳翻译器：决策都在 generation/ 包的 ProblemGenerationAgent 内（LLM 原创 →
重试 → LeetCode 拉题 → 历史未 AC → 静态题库）。此处 mock _GEN_AGENT 验证：

* 翻译成功（任意通道）→ goto wait_for_submit_node，welcome 与题目数据正确落位；
* 全通道失败 → 不落库、置 status=error 并以 error_message 友好提示用户；
* 命中通道（channel）落盘到 progress，供 serializer 透出前端。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.generation.state import (  # noqa: E402
    GenerationResult,
    ProblemDraft,
)
from code_tutor_agent.nodes import generator  # noqa: E402
from code_tutor_agent.schemas.state import Message as TutorMsg  # noqa: E402
from code_tutor_agent.schemas.state import SessionState  # noqa: E402


def _make_state() -> SessionState:
    return SessionState(session_id="sid-pathc", topic="数组", difficulty="easy", mode="practice")


def _draft(title: str = "Two Pointers Basics") -> ProblemDraft:
    return ProblemDraft(
        topic="数组",
        difficulty="easy",
        title=title,
        description="静态题面",
        starter_code="class Solution:\n    def f(self, a, b): pass\n",
        optimal_solution="class Solution:\n    def f(self, a, b): return a\n",
        function_signature="a: int, b: int -> int",
        test_cases=[
            {"input_args": ["[1,2]"], "expected_output": "[1,2]", "explanation": "s"},
        ],
    )


def _fake_agent(result: GenerationResult) -> SimpleNamespace:
    return SimpleNamespace(run=lambda ctx, sink=None: result)


def test_generator_uses_static_channel_result():
    """agent 返回 static 通道结果 → generator_node 采用并落库（welcome 为普通文案）。"""
    state = _make_state()
    result = GenerationResult(ok=True, channel="static", problem_id=9, draft=_draft())

    with patch.object(generator, "_GEN_AGENT", _fake_agent(result)), \
         patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    assert cmd.goto == "wait_for_submit_node"
    assert cmd.update["problem"].title == "Two Pointers Basics"
    assert cmd.update["problem"].problem_id == 9
    assert cmd.update["status"] == "awaiting_submit"
    assert cmd.update["tutor_messages"][0].content.startswith("来，试试这道")
    # 通道透出：static 结果写进 progress
    from code_tutor_agent.progress import get_generation_channel
    assert get_generation_channel("sid-pathc") == "static"


def test_generator_uses_leetcode_import_channel_result():
    """LC 导入通道 → welcome 带 LeetCode 铭牌。"""
    state = _make_state()
    result = GenerationResult(
        ok=True, channel="leetcode_import", problem_id=4,
        draft=_draft(title="Two Sum"),
    )
    with patch.object(generator, "_GEN_AGENT", _fake_agent(result)), \
         patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    assert cmd.goto == "wait_for_submit_node"
    assert "来自 LeetCode 的 **Two Sum**" in cmd.update["tutor_messages"][0].content


def test_generator_errors_cleanly_when_all_channels_fail():
    """全通道失败 → 不抛异常、不落库，status=error + 友好提示，通道不入库。"""
    state = SessionState(
        session_id="sid-pathc-fail", topic="数组", difficulty="easy", mode="practice",
    )
    result = GenerationResult(ok=False, channel=None, error="所有可用通道均失败")

    with patch.object(generator, "_GEN_AGENT", _fake_agent(result)), \
         patch.object(generator, "get_stream_writer", return_value=None):
        cmd = generator.generator_node(state)

    assert cmd.goto == "__end__"
    assert cmd.update["status"] == "error"
    assert "换个主题" in cmd.update["error_message"]
    from code_tutor_agent.progress import get_generation_channel
    assert get_generation_channel("sid-pathc-fail") == ""  # None 不透出


def test_generator_agent_mode_preserves_dialog():
    """agent 模式：出题前对话保留 + welcome 追加，channel 仍透出。"""
    from code_tutor_agent.schemas.state import Message as TutorMsg

    state = SessionState(
        session_id="sid-pathc-agent", topic="数组", difficulty="easy", mode="agent",
        tutor_messages=[
            TutorMsg(role="tutor", content="我们来做一道数组题吧"),
            TutorMsg(role="user", content="好呀"),
        ],
    )
    result = GenerationResult(ok=True, channel="llm", problem_id=2, draft=_draft())
    with patch.object(generator, "_GEN_AGENT", _fake_agent(result)), \
         patch.object(generator, "get_stream_writer", return_value=None), \
         patch.object(generator, "get_struct_prologue", return_value=""):
        cmd = generator.generator_node(state)

    msgs = cmd.update["tutor_messages"]
    assert msgs[0].content == "我们来做一道数组题吧"
    assert msgs[-1].content.startswith("来，试试这道")
    from code_tutor_agent.progress import get_generation_channel
    assert get_generation_channel("sid-pathc-agent") == "llm"


def test_generator_agent_mode_leetcode_failure_returns_to_dialog():
    """agent 模式 LeetCode 导入失败 → 回对话态（status=dialog），不跳错误屏、会话继续。

    对齐"贴了非 LeetCode 链接"的 case：解析失败不卡住、不跳错误屏，而是补一句
    友好提示、清空 leetcode 标记让会话继续（避免下一轮对话又触发同一失败链接）。
    """
    state = SessionState(
        session_id="sid-pathc-agent-lc-fail", topic="数组", difficulty="easy", mode="agent",
        tutor_messages=[
            TutorMsg(role="tutor", content="已识别 LeetCode 链接，正在导入…"),
        ],
    )
    result = GenerationResult(
        ok=False, channel="leetcode_import",
        error="无法获取 LeetCode 题目（Problem 'add-three-numbers' not found）",
    )
    with patch.object(generator, "_GEN_AGENT", _fake_agent(result)), \
         patch.object(generator, "get_stream_writer", return_value=None):
        cmd = generator.generator_node(state)

    assert cmd.goto == "__end__"
    assert cmd.update["status"] == "dialog"            # 回对话态，不跳错误屏
    assert cmd.update.get("error_message", "") == ""   # 不置 error_message
    assert cmd.update.get("leetcode") is None          # 清空，避免下一轮重触发
    assert cmd.update.get("agent_dialog_complete") is False
    # 友好提示已追加到 tutor_messages（会话继续）
    assert any("解析失败" in m.content for m in cmd.update["tutor_messages"])
