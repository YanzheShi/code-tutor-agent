"""Agent 工具集 — 包本地 Python 函数为 LangChain ``StructuredTool``。

设计为「首批接入 tool calling 的两个能力域」：

1. **解析 LeetCode**：``parse_leetcode`` —— 包 ``leetcode/leetcode_fetcher.py``
   的 ``fetch_problem`` / ``problem_to_api_dict``。
2. **判题 Judge0**：``judge_run_code`` / ``judge_code`` / ``judge_check_health``
   —— 包 ``sandbox/judge0_client.py`` 的 ``run_code`` / ``submit_test_cases`` /
   ``check_health``。

这些底层函数全是本项目自己的本地同步函数（urllib / 网络），故用本地
``bind_tools`` 直接绑，**不**起 MCP 子进程；同步调用统一用 ``asyncio.to_thread``
包一层，避免阻塞 agent 的事件循环。

> 关键边界（详见 docs/agent-leetcode-toolcall-design.md §2.3）：
> - ``parse_leetcode`` 在**对话意图分析**阶段使用（agent 自主决定解析题目）。
> - ``judge_*`` 工具在**辅导**环节使用（agent 现场跑代码验证/演示），
>   不取代图节点里确定性的批量判题流水线。
"""

from __future__ import annotations

import asyncio
import json
import re

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
import sys as _sys

# 本模块引用，供 run_tool_loop 动态解析工具函数（便于测试 mock）
_self_module = _sys.modules[__name__]

from code_tutor_agent.leetcode.leetcode_fetcher import (
    fetch_problem,
    problem_to_api_dict,
)
from code_tutor_agent.sandbox.judge0_client import (
    run_code,
    submit_test_cases,
    check_health,
)
# Phase 2（DP-1）：import 主通道。tools 是 LLM 调用 skill 的唯一封装层，
# 由 mode 决定走 adapter（默认）还是 opt-in CLI 逃生舱。
from code_tutor_agent.skills import engine_adapter as _engine_adapter

import logging as _logging
logger = _logging.getLogger(__name__)

# 当前题上下文（请求级）：由 chat.py 在跑工具循环前写入，
# 供下方 *_via_skill 在日志里回溯「这条题解/这道题是哪个 problem 的」。
# 默认空 dict，未设置时不报错。
from contextvars import ContextVar
current_problem_ctx: ContextVar[dict] = ContextVar("current_problem_ctx", default={})


# ──────────────────────────────────────────────
#  解析 LeetCode
# ──────────────────────────────────────────────


def _parse_leetcode(url: str) -> str:
    """Synchronous core: extract slug, fetch, and serialize to JSON string."""
    m = re.search(r"/problems/([^/]+)", url.strip().rstrip("/"))
    if not m:
        return json.dumps({"error": "无效的 LeetCode 链接，请粘贴完整题目 URL（含 /problems/xxx）"})
    slug = m.group(1)
    domain = "leetcode.cn" if ".cn" in url else "leetcode.com"
    try:
        p = fetch_problem(slug, domain=domain)
        return json.dumps(problem_to_api_dict(p), ensure_ascii=False)
    except Exception as e:  # 网络失败 / 429 / 题目不存在 → 转成 error JSON，不崩
        return json.dumps({"error": f"获取题目失败: {e}"})


async def parse_leetcode(url: str) -> str:
    """当用户提供 LeetCode 题目链接时调用，解析并返回结构化题目数据。"""
    return await asyncio.to_thread(_parse_leetcode, url)


# ──────────────────────────────────────────────
#  判题 Judge0
# ──────────────────────────────────────────────


def _judge_run_code(source_code: str, stdin: str = "") -> str:
    """Synchronous core: run arbitrary code once and serialize the result."""
    r = run_code(source_code, stdin=stdin)
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
        func=parse_leetcode,
        name="parse_leetcode",
        description=(
            "当用户提供 LeetCode 题目链接（leetcode.com 或 leetcode.cn 的 "
            "/problems/xxx）时调用，解析并返回标题/描述/示例/约束/模板代码等结构化数据，"
            "用于直接作为练习题。不要在没有链接时调用。"
        ),
    ),
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


async def run_tool_loop(
    llm,
    messages: list,
    tools: list | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> list:
    """通用工具循环：让 LLM 自主决定是否调用工具，并把结果喂回上下文。

    Args:
        llm: 通过 ``get_llm(...)`` 创建的模型实例（内部会 ``bind_tools``）。
        messages: LangChain Message 列表（会就地追加 AI / Tool 消息）。
        tools: 要绑定的 ``StructuredTool`` 列表；默认 ``JUDGE_TOOLS``。
        max_rounds: 工具循环最大轮数，防止 LLM 反复调工具空转。

    Returns:
        更新后的 messages（已含工具结果），调用方应据其做最终流式 / 结构化输出。

    注意：工具函数通过 ``getattr`` 从本模块动态解析，便于测试中 mock。
    """
    if tools is None:
        tools = JUDGE_TOOLS
    if not tools:
        return messages

    allowed = {t.name for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    for _ in range(max_rounds):
        ai = llm_with_tools.invoke(messages)
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
    return messages


# ──────────────────────────────────────────────
#  skill-engine CLI 逃生舱
# ──────────────────────────────────────────────

from code_tutor_agent.agents.skill_cli import (
    generate_problem_via_skill_sync,
    generate_detailed_solution_via_skill_sync,
)


async def generate_problem_via_skill(
    topic: str, difficulty: str, *, mode: str = "adapter"
) -> str:
    """通过 skill-engine 生成练习题（备选出题通道）。

    默认走 import 主通道（``engine_adapter.generate_problem``，进程内、结构化、
    直接喂 DB）；仅当用户在对话中显式要求 ``mode="cli"`` 时，才回退到
    ``skill_cli`` 子进程逃生舱（1:1 复现 CI 行为 / 调试 skill）。

    异步包装：同步核心经 ``asyncio.to_thread`` 防阻塞事件循环。函数名与
    ``SKILL_TOOLS`` 里的工具名一致，便于 ``run_tool_loop`` 用 ``getattr`` 解析。

    任何通道失败都归一为 ``{"error": ...}`` JSON，不向外抛。
    """
    _ctx = current_problem_ctx.get()
    logger.info(
        "▶ 出题（skill）topic=%s difficulty=%s mode=%s problem=%s title=%s",
        topic, difficulty, mode, _ctx.get("problem_id"), _ctx.get("title"),
    )
    if mode == "cli":
        return await asyncio.to_thread(
            generate_problem_via_skill_sync, topic, difficulty
        )
    # 默认 adapter 主通道
    try:
        prob = await asyncio.to_thread(
            _engine_adapter.generate_problem, topic, difficulty, max_retries=1
        )
        return json.dumps(prob, ensure_ascii=False)
    except Exception as exc:  # adapter 任何异常都降级为 error JSON
        return json.dumps(
            {"error": f"adapter 出题失败: {exc}"}, ensure_ascii=False
        )


async def generate_detailed_solution_via_skill(
    description: str, *, mode: str = "adapter"
) -> str:
    """通过 skill-engine 为当前题生成详细题解（Markdown 文本）。

    默认走 import 主通道（``engine_adapter.generate_detailed_solution``）；
    仅当 ``mode="cli"`` 时回退到 ``skill_cli`` 子进程逃生舱。

    异步包装：同步核心经 ``asyncio.to_thread`` 防阻塞事件循环。函数名与
    ``AGENT_TOOLS`` 里的工具名一致，便于 ``run_tool_loop`` 用 ``getattr`` 解析。

    任何通道失败都归一为 ``{"error": ...}`` JSON，不向外抛。
    """
    _ctx = current_problem_ctx.get()
    logger.info(
        "▶ 题解生成（skill）mode=%s problem=%s title=%s len(desc)=%d",
        mode, _ctx.get("problem_id"), _ctx.get("title"), len(description or ""),
    )
    if mode == "cli":
        return await asyncio.to_thread(
            generate_detailed_solution_via_skill_sync, description
        )
    # 默认 adapter 主通道
    try:
        return await asyncio.to_thread(
            _engine_adapter.generate_detailed_solution, description
        )
    except Exception as exc:  # adapter 任何异常都降级为 error JSON
        return json.dumps(
            {"error": f"adapter 生成详细题解失败: {exc}"}, ensure_ascii=False
        )


# 注册进 AGENT_TOOLS，使 LLM 在对话中可应请求调用（函数定义在 import 之后，故此处追加）
AGENT_TOOLS.append(
    StructuredTool.from_function(
        func=generate_detailed_solution_via_skill,
        name="generate_detailed_solution_via_skill",
        description=(
            "为『当前已选中的题』生成详细、可教学的题解"
            "（多思路演进、复杂度分析、可运行代码、易错点、核心洞察），区别于仅给解题代码的 cta-generate-solution。"
            "当用户在对话中请求『讲讲这题 / 给个详细题解 / 讲讲思路』时调用，"
            "参数 description 传入该题的完整题面（来自已解析的题目）。"
            "默认走进程内 import 通道（adapter）；仅当用户显式要求 CLI / 调试 skill 时传 mode='cli'。"
        ),
    )
)


# 导师辅导环节工具集：judge_* 现场验证工具 + 详细题解生成工具。
# 注意：JUDGE_TOOLS 在上方定义时 generate_detailed_solution_via_skill 尚未 append 进
# AGENT_TOOLS，故此处在其之后重新聚合，供 chat.py 的辅导路径绑定。
_detailed_solution_tool = get_tool("generate_detailed_solution_via_skill")
TUTOR_TOOLS = JUDGE_TOOLS + ([_detailed_solution_tool] if _detailed_solution_tool else [])

# 交互式聊天循环用的轻量工具集：仅含本地沙箱判题验证工具（judge_*）。
# 故意排除 generate_detailed_solution_via_skill —— 该工具走 skill-engine 跑一次完整
# 题解 LLM 生成（默认 agnes 模型），单次耗时可达 90s，放进阻塞式 /chat/stream 回复会
# 把导师答复卡死（用户实测 60~90s 才出结果）。导师本身就能在回复里直接写代码 / 讲解，
# 无需为每次对话触发重型 skill 生成；详细题解走出题 / 专用入口按需生成即可。
TUTOR_CHAT_TOOLS = JUDGE_TOOLS


# 仅在「对话/需求澄清阶段」由 LLM 自主选择题型时使用，
# 默认不进 AGENT_TOOLS（避免辅导环节误暴露出题工具）。
SKILL_TOOLS = [
    StructuredTool.from_function(
        func=generate_problem_via_skill,
        name="generate_problem_via_skill",
        description=(
            "生成练习题（skill-engine 出题资产）。当用户在对话中明确要求"
            "用 skill-engine 出题、或点名某类题型（数学推导/科学计算/工程场景/AI 启发式）时调用。"
            "参数 topic 与 difficulty 由对话上下文决定。普通 LeetCode 风格出题不要用此工具。"
            "默认走进程内 import 通道（adapter）；仅当用户显式要求 CLI / 调试 skill 时传 mode='cli'。"
        ),
    ),
]
