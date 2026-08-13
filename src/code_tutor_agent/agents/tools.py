"""Agent 工具集 — 包本地 Python 函数为 LangChain ``StructuredTool``。

判题 Judge0 工具域（辅导环节 agent 现场跑代码验证/演示）：
- ``judge_run_code`` / ``judge_code`` / ``judge_check_health``
  —— 包 ``sandbox/judge0_client.py`` 的 ``run_code`` / ``submit_test_cases`` /
  ``check_health``。

这些底层函数全是本项目自己的本地同步函数（urllib / 网络），故用本地
``bind_tools`` 直接绑，**不**起 MCP 子进程；同步调用统一用 ``asyncio.to_thread``
包一层，避免阻塞 agent 的事件循环。

> 关键边界（详见 docs/agent-leetcode-toolcall-design.md §2.3）：
> - LeetCode **解析**已收口到 generation 包（generator 路径 A 服务端抓取），
>   不再是 agent 工具；对话意图分析阶段只负责识别链接并转发 URL。
> - ``judge_*`` 工具在**辅导**环节使用（agent 现场跑代码验证/演示），
>   不取代图节点里确定性的批量判题流水线。
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
import sys as _sys

# 本模块引用，供 run_tool_loop 动态解析工具函数（便于测试 mock）
_self_module = _sys.modules[__name__]

from code_tutor_agent.sandbox.judge0_client import (
    run_code,
    submit_test_cases,
    check_health,
    SandboxNotExecuted,
)

import logging as _logging
logger = _logging.getLogger(__name__)



# ──────────────────────────────────────────────
#  判题 Judge0
# ──────────────────────────────────────────────


def _judge_run_code(source_code: str, stdin: str = "") -> str:
    """Synchronous core: run arbitrary code once and serialize the result.

    沙箱不可用（提交未执行 / 网络不可达）时，返回带明确提示的 JSON，
    引导导师**基于算法知识直接回答**，而不是把「没跑」误判成「代码崩溃」
    或触发上层空回复兜底。
    """
    _SANDBOX_DOWN = (
        "代码验证沙箱当前不可用（提交后未执行或网络不可达）。"
        "请直接基于你的算法与数据结构知识回答用户的问题，"
        "不要依赖运行结果，也不要再调用任何 judge_* 验证工具。"
    )
    try:
        r = run_code(source_code, stdin=stdin)
    except (SandboxNotExecuted, RuntimeError) as exc:
        logger.warning("judge_run_code sandbox unavailable: %s", exc)
        return json.dumps(
            {"verdict": "NO_RUN", "status": "sandbox_unavailable",
             "error": str(exc), "message": _SANDBOX_DOWN},
            ensure_ascii=False,
        )
    if r.verdict() == "NO_RUN":
        return json.dumps(
            {"verdict": "NO_RUN", "status": "sandbox_unavailable",
             "error": r.status_desc or "empty status", "message": _SANDBOX_DOWN},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "stdout": r.stdout,
            "stderr": r.stderr,
            "status": r.status_desc,
            "verdict": r.verdict(),
            "time_ms": r.runtime_ms(),
            "memory_kb": r.memory_kilobytes,
        },
        ensure_ascii=False,
    )


async def judge_run_code(source_code: str, stdin: str = "") -> str:
    """执行任意 Python 代码片段并返回 stdout/stderr/状态，用于辅导时验证思路或演示。"""
    return await asyncio.to_thread(_judge_run_code, source_code, stdin)


def _judge_code(source_code: str, test_cases_json: str) -> str:
    """Synchronous core: run a batch of test cases and summarize pass/fail."""
    tcs = json.loads(test_cases_json) if isinstance(test_cases_json, str) else test_cases_json
    results = submit_test_cases(source_code, tcs)
    passed = sum(1 for r in results if r.get("status") == "Passed")
    return json.dumps(
        {
            "results": results,
            "summary": {
                "total": len(results),
                "passed": passed,
                "all_passed": passed == len(results),
            },
        },
        ensure_ascii=False,
    )


async def judge_code(source_code: str, test_cases_json: str) -> str:
    """用一批测试用例判题（LeetCode 风格 class Solution），返回每个用例通过与汇总。"""
    return await asyncio.to_thread(_judge_code, source_code, test_cases_json)


async def judge_check_health() -> str:
    """检查判题后端（Judge0）是否存活。"""
    return await asyncio.to_thread(lambda: json.dumps(check_health(), ensure_ascii=False))


# ──────────────────────────────────────────────
#  导出给 agent 绑定
# ──────────────────────────────────────────────


AGENT_TOOLS = [
    StructuredTool.from_function(
        func=judge_run_code,
        name="judge_run_code",
        description="执行任意 Python 代码片段并返回 stdout/stderr/状态，用于辅导时验证思路或演示。",
    ),
    StructuredTool.from_function(
        func=judge_code,
        name="judge_code",
        description="用一批测试用例判题(LeetCode 风格 class Solution)，返回每个用例通过与汇总。",
    ),
    StructuredTool.from_function(
        func=judge_check_health,
        name="judge_check_health",
        description="检查判题后端(Judge0)是否存活。",
    ),
]


def get_tool(name: str) -> StructuredTool | None:
    """按名称取工具，找不到返回 None。"""
    return next((t for t in AGENT_TOOLS if t.name == name), None)


# ──────────────────────────────────────────────
#  通用工具循环（辅导 / 对话共享）
# ──────────────────────────────────────────────

MAX_TOOL_ROUNDS = 3

# 导师辅导环节默认只暴露 judge 工具（解析出题由对话意图分析阶段负责）
JUDGE_TOOLS = [t for t in AGENT_TOOLS if t.name.startswith("judge_")]


def _msg_text(content) -> str:
    """把 LangChain message content（str / list[part]）归一为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                parts.append(c.get("text", ""))
            else:
                parts.append(str(c))
        return "".join(parts)
    return str(content)


async def run_tool_loop(
    llm,
    messages: list,
    tools: list | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    return_last_content: bool = False,
) -> "list | tuple[list, str]":
    """通用工具循环：让 LLM 自主决定是否调用工具，并把结果喂回上下文。

    Args:
        llm: 通过 ``get_llm(...)`` 创建的模型实例（内部会 ``bind_tools``）。
        messages: LangChain Message 列表（会就地追加 AI / Tool 消息）。
        tools: 要绑定的 ``StructuredTool`` 列表；默认 ``JUDGE_TOOLS``。
        max_rounds: 工具循环最大轮数，防止 LLM 反复调工具空转。
        return_last_content: 为 True 时返回 (messages, last_content) 元组，
            其中 last_content 是最后一轮 LLM 回复文本（无 tool_calls 的那轮）。
            供调用方在「纯讨论、未调工具」时直接复用，避免再发一次 LLM 调用。

    Returns:
        默认返回更新后的 messages（list）。
        return_last_content=True 时返回 (messages, last_content) 元组。

    注意：工具函数通过 ``getattr`` 从本模块动态解析，便于测试中 mock。
    """
    if tools is None:
        tools = JUDGE_TOOLS
    if not tools:
        if return_last_content:
            return messages, ""
        return messages

    allowed = {t.name for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    last_content = ""

    for _ in range(max_rounds):
        ai = llm_with_tools.invoke(messages)
        last_content = _msg_text(getattr(ai, "content", ""))
        tcs = getattr(ai, "tool_calls", None) or []
        if not tcs:
            break
        appended = False
        for tc in tcs:
            name = tc.get("name")
            if name not in allowed:
                continue
            fn = getattr(_self_module, name, None)
            if fn is None or not callable(fn):
                continue
            try:
                # 直接 await 原始 async 函数，绕过 StructuredTool.ainvoke 的协程坑
                out = await fn(**tc.get("args", {}))
            except Exception as e:  # 工具异常不崩，转成 error JSON
                out = json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)
            messages.append(ai)
            messages.append(ToolMessage(content=out, tool_call_id=tc["id"]))
            appended = True
        if not appended:
            break  # LLM 调了未绑定工具 → 停止，避免空转
    if return_last_content:
        return messages, last_content
    return messages


# ──────────────────────────────────────────────
#  导师辅导环节工具集
# ──────────────────────────────────────────────

# 交互式聊天循环用的轻量工具集：仅含本地沙箱判题验证工具（judge_*）。
# 导师本身就能在回复里直接写代码 / 讲解，无需额外生成工具。
TUTOR_CHAT_TOOLS = JUDGE_TOOLS
