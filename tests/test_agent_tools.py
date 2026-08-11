"""Phase 1 测试：agent tool calling 接入（解析 LeetCode + 判题 Judge0）。

全部外部依赖（LeetCode 网络、LLM、Judge0 后端）均被 mock，
保证在无网络 / 无 API key 环境下也能稳定跑绿。

覆盖点：
- 四个工具各自的序列化逻辑（mock 底层同步函数）
- ``AGENT_TOOLS`` 注册表完整性
- ``_extract_leetcode_url`` 链接识别
- ``analyze_user_intent``：贴 LeetCode 链接 → 短链返回 source=leetcode（解析收口到 generation）；
  无链接时走 LLM 结构化输出 + 兜底解析
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from code_tutor_agent.agents.tools import (
    judge_run_code,
    judge_code,
    judge_check_health,
    AGENT_TOOLS,
    get_tool,
)
from code_tutor_agent.agents.agent_dialog import (
    analyze_user_intent,
    _extract_leetcode_url,
    DialogIntent,
)
from code_tutor_agent.schemas.state import Message
from code_tutor_agent.sandbox.judge0_client import Judge0SubmissionResult


# ──────────────────────────────────────────────
#  Fake LLM（替代 get_llm，零网络）
# ──────────────────────────────────────────────


class _FakeModel:
    """极简 fake：bind_tools / with_structured_output 都返回自身；

    invoke 第一次返回带 tool_calls 的 AIMessage，其余返回预设的 final intent。
    """

    def __init__(self, tool_calls: list | None, final_intent: DialogIntent):
        self._tool_calls = tool_calls or []
        self._final = final_intent
        self._calls = 0

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self._calls += 1
        if self._calls == 1 and self._tool_calls:
            return AIMessage(content="", tool_calls=self._tool_calls)
        return self._final


# ──────────────────────────────────────────────
#  工具：判题 Judge0
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_judge_run_code_serialization():
    raw = {
        "stdout": "3\n", "stderr": "", "status": {"id": 3, "description": "Accepted"},
        "time": "0.012", "memory": "1024",
    }
    with patch("code_tutor_agent.agents.tools.run_code",
               return_value=Judge0SubmissionResult(raw)):
        out = await judge_run_code("print(3)")
    data = json.loads(out)
    assert data["stdout"] == "3\n"
    assert data["verdict"] == "AC"
    assert data["time_ms"] == 12.0
    assert data["memory_kb"] == 1024.0


@pytest.mark.asyncio
async def test_judge_code_summary():
    tcs = [
        {"status": "Passed", "detail": "[0,1]", "test_case_id": 0, "runtime_ms": 1.0},
        {"status": "Wrong Answer", "detail": "x", "test_case_id": 1, "runtime_ms": 1.0},
    ]
    with patch("code_tutor_agent.agents.tools.submit_test_cases", return_value=tcs):
        out = await judge_code("class Solution: pass", json.dumps([{"input_args": [], "expected_output": ""}]))
    data = json.loads(out)
    assert data["summary"]["total"] == 2
    assert data["summary"]["passed"] == 1
    assert data["summary"]["all_passed"] is False


@pytest.mark.asyncio
async def test_judge_check_health_passthrough():
    with patch("code_tutor_agent.agents.tools.check_health",
               return_value={"status": "ok", "workers_alive": 2}):
        out = await judge_check_health()
    data = json.loads(out)
    assert data["status"] == "ok"


# ──────────────────────────────────────────────
#  注册表 / 链接识别
# ──────────────────────────────────────────────


def test_agent_tools_registry():
    names = {t.name for t in AGENT_TOOLS}
    # LeetCode 解析已收口到 generation 包，不再作为 agent 工具
    assert names == {
        "judge_run_code",
        "judge_code",
        "judge_check_health",
    }


def test_extract_leetcode_url():
    hist = [
        Message(role="tutor", content="你好"),
        Message(role="user", content="帮我做 https://leetcode.cn/problems/two-sum 这道题"),
    ]
    assert _extract_leetcode_url(hist) == "https://leetcode.cn/problems/two-sum"

    hist2 = [Message(role="user", content="我想练数组题")]
    assert _extract_leetcode_url(hist2) is None


# ──────────────────────────────────────────────
#  analyze_user_intent 工具循环（mock LLM）
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_leetcode_url_short_circuits():
    """贴 LeetCode 链接 → analyze_user_intent 直接短链返回 source=leetcode。

    解析已收口到 generator_node，此层只透传 URL、不再调 LLM 解析工具；
    故即便把 get_llm patch 成"返回 generated 意图"，结果也必须是 leetcode 短链。
    """
    fake = _FakeModel([], DialogIntent(topic="数组", difficulty="medium", is_ready=True, source="generated"))

    history = [Message(role="user", content="帮我做 https://leetcode.cn/problems/two-sum 这道题")]
    with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=fake), \
         patch("code_tutor_agent.agents.agent_dialog._build_transcript", return_value="transcript"), \
         patch("code_tutor_agent.agents.agent_dialog._build_profile_summary", return_value=""):
        intent = await analyze_user_intent(history)

    # 短链返回：source=leetcode，URL 透传，is_ready=True
    assert intent.source == "leetcode"
    assert intent.leetcode_url == "https://leetcode.cn/problems/two-sum"
    assert intent.is_ready is True
    # 未走 LLM 结构化路径：fake 的 final intent 不应被采用（topic 应为空）
    assert intent.topic == ""
    # LLM 在短链返回前根本没被调用
    assert fake._calls == 0


@pytest.mark.asyncio
async def test_analyze_no_preference_auto_selects_topic():
    """用户连续 2+ 轮"随便" → 自动选弱项 topic，is_ready=True，不再追问。

    替换原 test_analyze_parse_error_keeps_default_source：解析失败兜底已不存在
    （解析收口到 generator_node），这里转而覆盖"无偏好自动选题"这一独立分支。
    """
    history = [
        Message(role="user", content="随便"),
        Message(role="tutor", content="那你想练哪个方向？"),
        Message(role="user", content="都可以"),
        Message(role="tutor", content="再想想？"),
        Message(role="user", content="你定吧"),
    ]
    fake = _FakeModel([], DialogIntent(topic="", difficulty="", is_ready=False))
    with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=fake), \
         patch("code_tutor_agent.agents.agent_dialog._build_profile_summary",
               return_value="- **数组**：熟练度很低(prof=0.10)"):
        intent = await analyze_user_intent(history)

    assert intent.is_ready is True
    assert intent.difficulty == "medium"
    assert intent.topic == "数组"  # _pick_auto_topic 从画像弱项取
    # 自动选路在 LLM 之前返回，未调用 LLM
    assert fake._calls == 0


@pytest.mark.asyncio
async def test_analyze_no_url_no_tool():
    # 没有链接 → 不绑工具 → LLM 直接出结构化意图
    fake = _FakeModel([], DialogIntent(topic="动态规划", difficulty="hard", is_ready=True))
    history = [Message(role="user", content="我想练动态规划，困难难度")]
    with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=fake), \
         patch("code_tutor_agent.agents.agent_dialog._build_transcript", return_value="transcript"), \
         patch("code_tutor_agent.agents.agent_dialog._build_profile_summary", return_value=""):
        intent = await analyze_user_intent(history)

    assert intent.source == "generated"
    assert intent.topic == "动态规划"
    assert intent.is_ready is True


# ──────────────────────────────────────────────
#  Phase 2：run_tool_loop 通用工具循环（导师辅导环节）
# ──────────────────────────────────────────────


def test_judge_tools_excludes_parse():
    """导师辅导环节只暴露 judge 工具，不应暴露解析出题工具。"""
    from code_tutor_agent.agents.tools import JUDGE_TOOLS

    names = {t.name for t in JUDGE_TOOLS}
    assert names == {"judge_run_code", "judge_code", "judge_check_health"}
    assert "parse_leetcode" not in names


class _ToolLoopFakeLLM:
    """invoke 第 1 次返回预设 tool_calls，之后返回空（让循环收敛）。"""

    def __init__(self, first_tool_calls=None):
        self._first = first_tool_calls or []
        self._n = 0
        self.invoked = []

    def bind_tools(self, tools):
        self._bound = {t.name for t in tools}
        return self

    def invoke(self, messages):
        self._n += 1
        self.invoked.append(messages)
        if self._n == 1 and self._first:
            return AIMessage(content="", tool_calls=self._first)
        return AIMessage(content="final", tool_calls=[])


@pytest.mark.asyncio
async def test_run_tool_loop_invokes_judge_tool():
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, JUDGE_TOOLS

    tcs = [{"name": "judge_run_code", "args": {"source_code": "print(42)"}, "id": "t1"}]
    fake_llm = _ToolLoopFakeLLM(tcs)
    msgs = [SystemMessage(content="sys"), HumanMessage(content="run this")]

    async def _fake_judge(source_code, stdin=""):
        return '{"stdout": "42"}'

    with patch("code_tutor_agent.agents.tools.judge_run_code", side_effect=_fake_judge):
        result = await run_tool_loop(fake_llm, msgs, tools=JUDGE_TOOLS)

    # 原 2 条 + AI(message 带 tool_calls) + ToolMessage
    assert len(result) == 4
    from langchain_core.messages import ToolMessage
    assert isinstance(result[-1], ToolMessage)
    assert json.loads(result[-1].content)["stdout"] == "42"
    assert result[-1].tool_call_id == "t1"
    # 工具函数被真正 await 调用过
    assert fake_llm._n >= 2  # 首轮调工具 + 次轮收敛


@pytest.mark.asyncio
async def test_run_tool_loop_no_tools_leaves_messages_unchanged():
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, JUDGE_TOOLS

    fake_llm = _ToolLoopFakeLLM([])  # 无 tool_calls
    msgs = [SystemMessage(content="s"), HumanMessage(content="hi")]
    result = await run_tool_loop(fake_llm, msgs, tools=JUDGE_TOOLS)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_run_tool_loop_tool_error_becomes_json():
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, JUDGE_TOOLS

    tcs = [{"name": "judge_run_code", "args": {"source_code": "x"}, "id": "t1"}]
    fake_llm = _ToolLoopFakeLLM(tcs)
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]

    async def _boom(source_code, stdin=""):
        raise RuntimeError("judge down")

    with patch("code_tutor_agent.agents.tools.judge_run_code", side_effect=_boom):
        result = await run_tool_loop(fake_llm, msgs, tools=JUDGE_TOOLS)

    assert len(result) == 4
    from langchain_core.messages import ToolMessage
    assert isinstance(result[-1], ToolMessage)
    assert "error" in json.loads(result[-1].content)


@pytest.mark.asyncio
async def test_run_tool_loop_ignores_unbound_tool():
    """LLM 调了未绑定工具（如 unknown_tool）→ 不应执行，循环应立即停。"""
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, JUDGE_TOOLS

    tcs = [{"name": "unknown_tool", "args": {"url": "x"}, "id": "t1"}]
    fake_llm = _ToolLoopFakeLLM(tcs)
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    result = await run_tool_loop(fake_llm, msgs, tools=JUDGE_TOOLS)
    assert len(result) == 2  # 未绑工具 → 不追加，messages 不变


@pytest.mark.asyncio
async def test_tutor_chat_tools_are_judge_only():
    """TUTOR_CHAT_TOOLS 仅含 judge 工具，不含解析出题工具。"""
    from code_tutor_agent.agents.tools import TUTOR_CHAT_TOOLS, JUDGE_TOOLS

    names = {t.name for t in TUTOR_CHAT_TOOLS}
    assert names == {
        "judge_run_code",
        "judge_code",
        "judge_check_health",
    }
    assert set(names) >= {t.name for t in JUDGE_TOOLS}
    assert "parse_leetcode" not in names
