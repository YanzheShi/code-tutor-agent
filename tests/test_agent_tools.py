"""Phase 1 测试：agent tool calling 接入（解析 LeetCode + 判题 Judge0）。

全部外部依赖（LeetCode 网络、LLM、Judge0 后端）均被 mock，
保证在无网络 / 无 API key 环境下也能稳定跑绿。

覆盖点：
- 四个工具各自的序列化逻辑（mock 底层同步函数）
- ``AGENT_TOOLS`` 注册表完整性
- ``_extract_leetcode_url`` 链接识别
- ``analyze_user_intent`` 工具循环：贴链接 → 解析 → 意图带 leetcode_payload；
  以及解析失败 / 无链接两种兜底路径
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from langchain_core.messages import AIMessage

from code_tutor_agent.agents.tools import (
    parse_leetcode,
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
from code_tutor_agent.skills.result import SkillResult
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


def _lc_payload(title="Two Sum", difficulty="easy") -> str:
    return json.dumps({
        "title": title,
        "difficulty": difficulty,
        "description": "d",
        "examples": [],
        "constraints": [],
        "starter_code": "",
        "hints": [],
        "tags": ["数组"],
        "parsed_test_cases": [],
    })


# ──────────────────────────────────────────────
#  工具：解析 LeetCode
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_leetcode_success():
    fake_problem = MagicMock()
    with patch("code_tutor_agent.agents.tools.fetch_problem", return_value=fake_problem), \
         patch("code_tutor_agent.agents.tools.problem_to_api_dict",
               return_value=json.loads(_lc_payload("Two Sum", "easy"))):
        out = await parse_leetcode("https://leetcode.cn/problems/two-sum")
    data = json.loads(out)
    assert data["title"] == "Two Sum"
    assert data["difficulty"] == "easy"
    assert "error" not in data


@pytest.mark.asyncio
async def test_parse_leetcode_invalid_url():
    out = await parse_leetcode("这不是一个链接")
    data = json.loads(out)
    assert "error" in data


@pytest.mark.asyncio
async def test_parse_leetcode_domain_detection():
    captured = {}

    def _fake_fetch(slug, domain="leetcode.cn"):
        captured["slug"] = slug
        captured["domain"] = domain
        return MagicMock()

    with patch("code_tutor_agent.agents.tools.fetch_problem", side_effect=_fake_fetch), \
         patch("code_tutor_agent.agents.tools.problem_to_api_dict",
               return_value=json.loads(_lc_payload())):
        await parse_leetcode("https://leetcode.com/problems/best-time-to-buy-and-sell-stock")
    assert captured["slug"] == "best-time-to-buy-and-sell-stock"
    assert captured["domain"] == "leetcode.com"


@pytest.mark.asyncio
async def test_parse_leetcode_network_error_returns_error_json():
    with patch("code_tutor_agent.agents.tools.fetch_problem",
               side_effect=ValueError("not found")):
        out = await parse_leetcode("https://leetcode.cn/problems/does-not-exist")
    data = json.loads(out)
    assert "error" in data


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
    assert names == {
        "parse_leetcode",
        "judge_run_code",
        "judge_code",
        "judge_check_health",
        "generate_detailed_solution_via_skill",
    }
    # parse_leetcode 工具应声明 url 参数
    tool = get_tool("parse_leetcode")
    assert tool is not None
    assert "url" in tool.args


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
async def test_analyze_triggers_parse_leetcode():
    tool_calls = [{
        "name": "parse_leetcode",
        "args": {"url": "https://leetcode.cn/problems/two-sum"},
        "id": "c1",
    }]
    fake = _FakeModel(tool_calls, DialogIntent(topic="数组", difficulty="medium", is_ready=True))

    history = [Message(role="user", content="帮我做 https://leetcode.cn/problems/two-sum")]
    with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=fake), \
         patch("code_tutor_agent.agents.agent_dialog._build_transcript", return_value="transcript"), \
         patch("code_tutor_agent.agents.agent_dialog._build_profile_summary", return_value=""), \
         patch("code_tutor_agent.agents.tools.fetch_problem", return_value=MagicMock()), \
         patch("code_tutor_agent.agents.tools.problem_to_api_dict",
               return_value=json.loads(_lc_payload("Two Sum", "easy"))):
        intent = await analyze_user_intent(history)

    assert intent.source == "leetcode"
    assert intent.leetcode_url == "https://leetcode.cn/problems/two-sum"
    assert "Two Sum" in intent.leetcode_payload
    assert intent.is_ready is True


@pytest.mark.asyncio
async def test_analyze_parse_error_keeps_default_source():
    tool_calls = [{
        "name": "parse_leetcode",
        "args": {"url": "https://leetcode.cn/problems/broken"},
        "id": "c1",
    }]
    fake = _FakeModel(tool_calls, DialogIntent(topic="数组", difficulty="medium", is_ready=True))

    history = [Message(role="user", content="https://leetcode.cn/problems/broken")]
    with patch("code_tutor_agent.agents.agent_dialog.get_llm", return_value=fake), \
         patch("code_tutor_agent.agents.agent_dialog._build_transcript", return_value="transcript"), \
         patch("code_tutor_agent.agents.agent_dialog._build_profile_summary", return_value=""), \
         patch("code_tutor_agent.agents.tools.fetch_problem",
               side_effect=ValueError("boom")):
        intent = await analyze_user_intent(history)

    # 解析失败 → 不把坏数据塞进 payload，source 保持默认
    assert intent.source == "generated"
    assert intent.leetcode_payload == ""


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
    """LLM 调了未绑定工具（如 parse_leetcode）→ 不应执行，循环应立即停。"""
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, JUDGE_TOOLS

    tcs = [{"name": "parse_leetcode", "args": {"url": "x"}, "id": "t1"}]
    fake_llm = _ToolLoopFakeLLM(tcs)
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    result = await run_tool_loop(fake_llm, msgs, tools=JUDGE_TOOLS)
    assert len(result) == 2  # 未绑工具 → 不追加，messages 不变


@pytest.mark.asyncio
async def test_tutor_tools_includes_detailed_solution():
    """TUTOR_TOOLS 含 judge 工具 + 详细题解生成工具，不含解析出题工具。"""
    from code_tutor_agent.agents.tools import TUTOR_TOOLS, JUDGE_TOOLS

    names = {t.name for t in TUTOR_TOOLS}
    assert names == {
        "judge_run_code",
        "judge_code",
        "judge_check_health",
        "generate_detailed_solution_via_skill",
    }
    assert set(names) >= {t.name for t in JUDGE_TOOLS}
    assert "parse_leetcode" not in names


@pytest.mark.asyncio
async def test_run_tool_loop_invokes_detailed_solution_via_tutor_tools():
    """辅导工具循环：LLM 调 generate_detailed_solution_via_skill → 执行并回写结果。"""
    from langchain_core.messages import SystemMessage, HumanMessage
    from code_tutor_agent.agents.tools import run_tool_loop, TUTOR_TOOLS

    tcs = [{
        "name": "generate_detailed_solution_via_skill",
        "args": {"description": "给定一个数组 nums，求两数之和的下标。", "mode": "cli"},
        "id": "t1",
    }]
    fake_llm = _ToolLoopFakeLLM(tcs)
    msgs = [SystemMessage(content="sys"), HumanMessage(content="讲讲这题")]
    md = "# 思路一\n## Code\n```python\nclass Solution: ...\n```"

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-detailed-solution", ok=True, output=md,
        ),
    ):
        result = await run_tool_loop(fake_llm, msgs, tools=TUTOR_TOOLS)

    assert len(result) == 4
    from langchain_core.messages import ToolMessage
    assert isinstance(result[-1], ToolMessage)
    assert md in result[-1].content


# ──────────────────────────────────────────────
#  skill-engine CLI 逃生舱工具
# ──────────────────────────────────────────────


def test_skill_tools_registry():
    """SKILL_TOOLS 只含 generate_problem_via_skill，且默认不进 AGENT_TOOLS。"""
    from code_tutor_agent.agents.tools import SKILL_TOOLS

    assert {t.name for t in SKILL_TOOLS} == {"generate_problem_via_skill"}
    assert "generate_problem_via_skill" not in {t.name for t in AGENT_TOOLS}


_CONTRACT_MD = """\
==== result ====
## Title
Move Zeroes
## Topic
数组
## Difficulty
easy
## Description
将数组中所有 0 移动到末尾。
## Examples
Example 1:
Input: nums = [0,1,0,3,2]
Output: [1,3,2,0,0]
## StarterCode
```python
class Solution:
    def moveZeroes(self, nums): pass
```
## OptimalSolution
```python
class Solution:
    def moveZeroes(self, nums): pass
```
"""


@pytest.mark.asyncio
async def test_generate_problem_via_skill_success():
    """CLI 逃生舱出题成功 → 返回带 title 的 JSON，无 error。"""
    from code_tutor_agent.agents.tools import generate_problem_via_skill

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-problem", ok=True, output=_CONTRACT_MD,
        ),
    ):
        out = await generate_problem_via_skill("数组", "easy", mode="cli")
    data = json.loads(out)
    assert data["title"] == "Move Zeroes"
    assert "error" not in data


@pytest.mark.asyncio
async def test_generate_problem_via_skill_cli_failure_returns_error_json():
    """CLI 执行失败 → 转成 {"error": ...} JSON，不抛异常。"""
    from code_tutor_agent.agents.tools import generate_problem_via_skill

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-problem", ok=False, error="boom",
        ),
    ):
        out = await generate_problem_via_skill("数组", "easy", mode="cli")
    data = json.loads(out)
    assert "error" in data
    assert "CLI 出题失败" in data["error"]


@pytest.mark.asyncio
async def test_generate_problem_via_skill_parse_failure_returns_error_json():
    """CLI 成功但契约解析失败（stdout 无 ## 节）→ 转 error JSON。"""
    from code_tutor_agent.agents.tools import generate_problem_via_skill

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-problem", ok=True, output="no contract here",
        ),
    ):
        out = await generate_problem_via_skill("数组", "easy", mode="cli")
    data = json.loads(out)
    assert "error" in data
    assert "契约解析失败" in data["error"]


@pytest.mark.asyncio
async def test_agent_tools_registry_includes_detailed_solution():
    """generate_detailed_solution_via_skill 已注册进 AGENT_TOOLS，LLM 可见可调。"""
    from code_tutor_agent.agents.tools import AGENT_TOOLS

    assert "generate_detailed_solution_via_skill" in {t.name for t in AGENT_TOOLS}


@pytest.mark.asyncio
async def test_generate_detailed_solution_via_skill_success():
    """CLI 逃生舱生成详细题解成功 → 返回 Markdown 原文（已 strip）。"""
    from code_tutor_agent.agents.tools import generate_detailed_solution_via_skill

    md = "# 思路一：暴力\n## Code\n```python\nclass Solution: ...\n```"
    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-detailed-solution", ok=True, output="  " + md + "\n",
        ),
    ):
        out = await generate_detailed_solution_via_skill("题面描述", mode="cli")
    assert out == md


@pytest.mark.asyncio
async def test_generate_detailed_solution_via_skill_cli_failure():
    """CLI 失败 → 转成 {"error": ...} JSON，不抛异常。"""
    from code_tutor_agent.agents.tools import generate_detailed_solution_via_skill

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-detailed-solution", ok=False, error="boom",
        ),
    ):
        out = await generate_detailed_solution_via_skill("题面描述", mode="cli")
    import json
    data = json.loads(out)
    assert "error" in data
    assert "生成详细题解失败" in data["error"]


@pytest.mark.asyncio
async def test_generate_problem_via_skill_default_adapter():
    """默认 mode=adapter → 调 engine_adapter.generate_problem，返回含 title 的 JSON。"""
    from code_tutor_agent.agents.tools import generate_problem_via_skill

    fake_prob = {
        "title": "Move Zeroes", "topic": "数组", "difficulty": "easy",
        "description": "将 0 移到末尾", "starter_code": "",
        "optimal_solution": "", "test_cases": [],
    }
    with patch(
        "code_tutor_agent.skills.engine_adapter.generate_problem",
        return_value=fake_prob,
    ):
        out = await generate_problem_via_skill("数组", "easy")
    data = json.loads(out)
    assert data["title"] == "Move Zeroes"
    assert "error" not in data


@pytest.mark.asyncio
async def test_generate_problem_via_skill_adapter_failure_returns_error_json():
    """adapter 通道异常 → 归一为 {"error": ...} JSON，不冒泡。"""
    from code_tutor_agent.agents.tools import generate_problem_via_skill

    with patch(
        "code_tutor_agent.skills.engine_adapter.generate_problem",
        side_effect=RuntimeError("boom"),
    ):
        out = await generate_problem_via_skill("数组", "easy")
    data = json.loads(out)
    assert "error" in data
    assert "adapter 出题失败" in data["error"]


@pytest.mark.asyncio
async def test_generate_detailed_solution_via_skill_default_adapter():
    """默认 mode=adapter → 调 engine_adapter.generate_detailed_solution，返回 markdown。"""
    from code_tutor_agent.agents.tools import generate_detailed_solution_via_skill

    md = "# 思路一\n## Code\n```python\nclass Solution: ...\n```"
    with patch(
        "code_tutor_agent.skills.engine_adapter.generate_detailed_solution",
        return_value=md,
    ):
        out = await generate_detailed_solution_via_skill("题面描述")
    assert out == md


@pytest.mark.asyncio
async def test_generate_detailed_solution_via_skill_adapter_failure():
    """adapter 通道异常 → 归一为 {"error": ...} JSON，不冒泡。"""
    from code_tutor_agent.agents.tools import generate_detailed_solution_via_skill

    with patch(
        "code_tutor_agent.skills.engine_adapter.generate_detailed_solution",
        side_effect=RuntimeError("boom"),
    ):
        out = await generate_detailed_solution_via_skill("题面描述")
    data = json.loads(out)
    assert "error" in data
    assert "adapter 生成详细题解失败" in data["error"]
