"""后台生成服务：执行 graph.invoke + 随机测试用例 + LLM 边界用例生成。"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.generation import ProblemGenerationAgent
from code_tutor_agent.generation.state import GenEvent
from code_tutor_agent.generation.suite import build_suite
from code_tutor_agent.observability import build_run_config
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.state import Message as TutorMsg
from code_tutor_agent.schemas.state import ProblemMeta, SessionState

logger = logging.getLogger(__name__)

# 题目生成超时（秒），可通过环境变量覆盖
GENERATION_TIMEOUT = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120"))

# 后台套件生成的统一执行器（graph.invoke 后由 API 层调度，设计 §13）
_SUITE_AGENT = ProblemGenerationAgent()


class _ProgressSink:
    """把 suite 的 GenEvent 追加到会话进度（API 无 stream writer 的上下文）。"""

    _PREFIX = {"progress": "", "warning": "⚠️ ", "error": "❌ ", "info": "📝 "}

    def __init__(self, sid: str) -> None:
        self._sid = sid

    def event(self, ev: GenEvent) -> None:
        _generation_progress.setdefault(self._sid, []).append(
            f"{self._PREFIX.get(ev.kind, '')}{ev.message}"
        )


def _extract_code_from_llm_response(text: str) -> str:
    """Extract Python code from LLM response — strip markdown fences if present."""
    m = re.search(r"```python\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


async def _generate_optimal_for_leetcode_async(problem_id: int, sid: str):
    """Background task: call LLM to generate optimal_solution for a LeetCode-imported problem."""
    from code_tutor_agent.config import get_llm
    from code_tutor_agent.db.database import get_problem_by_id, update_problem_optimal_solution

    full = get_problem_by_id(problem_id)
    if not full:
        logger.warning("Problem %d not found for optimal solution generation", problem_id)
        return

    title = full.title
    description = full.description
    difficulty = full.difficulty
    func_sig = full.function_signature
    starter = full.starter_code

    logger.info("Generating optimal_solution for LeetCode problem '%s' (%d)", title, problem_id)

    prompt = (
        f"你是一个算法专家。给定以下 LeetCode 题目，请写出最优解 Python 代码（class Solution 风格）：\n\n"
        f"标题: {title}\n"
        f"描述: {description}\n"
        f"难度: {difficulty}\n"
    )
    if func_sig:
        prompt += f"函数签名: {func_sig}\n"
    if starter:
        prompt += f"模板代码:\n{starter}\n"
    prompt += (
        "\n要求：\n"
        "- 使用最优算法（如哈希表、双指针、动态规划等）\n"
        "- 必须是可运行的合法 Python 代码\n"
        "- 方法签名必须准确\n"
        "- 只输出代码，不要任何解释\n"
    )

    try:
        llm = get_llm(purpose="api-generation")
        resp = llm.invoke([("human", prompt)])
        code = resp.content if hasattr(resp, "content") else str(resp)
        code = _extract_code_from_llm_response(code)

        update_problem_optimal_solution(problem_id, code)
        _generation_progress.setdefault(sid, []).append(
            f"🤖 已生成最优解代码（{len(code)} 字符）"
        )
        logger.info("Generated optimal_solution for LeetCode problem %d (%d chars)", problem_id, len(code))
    except Exception as exc:
        logger.warning("Failed to generate optimal_solution for LeetCode problem %d: %s", problem_id, exc)


async def run_generation(sid: str, initial_dict: dict):
    """Full graph invoke in background with timeout, then generate complex tests.

    如果 graph 生成超时或 LLM 调用失败，自动降级到静态题库（static pool）。
    前端通过 progress_messages 看到完整过程。
    """
    graph = get_graph()
    config = build_run_config(
        sid,
        topic=initial_dict.get("topic"),
        difficulty=initial_dict.get("difficulty"),
        run_name="generate_problem",
    )

    try:
        initial = SessionState(**initial_dict)
        _generation_progress.setdefault(sid, []).append("\U0001f680 开始生成题目...")

        # 带超时的 graph invoke：不能在生成阶段无限等待 LLM
        await asyncio.wait_for(
            asyncio.to_thread(graph.invoke, initial.model_dump(), config),
            timeout=GENERATION_TIMEOUT,
        )
        _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪，正在后台生成完整测试用例...")

        try:
            state = graph.get_state(config)
            problem = state.values.get("problem")
            if problem:
                pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
                if pid:
                    # 彻底独立于 run_generation 生命周期：题目就绪后即可进做题，
                    # 复杂测试用例在独立 task 中后台生成（不阻塞本协程返回）。
                    asyncio.create_task(_run_suite_safe(pid, sid))
                else:
                    _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪")
            else:
                _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪")
        except Exception as exc:
            logger.warning("Background test generation failed: %s", exc)
            _generation_progress.setdefault(sid, []).append("\u26a0\ufe0f 部分测试用例生成失败")

    except asyncio.TimeoutError:
        logger.error("Generation timed out for %s after %ds, falling back to static pool", sid, GENERATION_TIMEOUT)
        _generation_progress.setdefault(sid, []).append(
            f"\u23f0 生成超时（{GENERATION_TIMEOUT}秒），LLM 响应太慢，正在从备用题库选题..."
        )
        await _fallback_static_problem(sid, config, initial_dict)

    except Exception as exc:
        logger.exception("Background generation failed for %s, falling back to static pool", sid)
        _generation_progress.setdefault(sid, []).append(
            f"\u274c LLM 生成失败（{_safe_err_msg(exc)}），正在从备用题库选题..."
        )
        await _fallback_static_problem(sid, config, initial_dict)


def _safe_err_msg(exc: Exception) -> str:
    """截取异常信息的前 80 个字符，避免在 UI 展示过长的调用栈。"""
    msg = str(exc)
    return msg[:80] + "..." if len(msg) > 80 else msg


async def _fallback_static_problem(sid: str, config: dict, initial_dict: dict):
    """降级策略：从本地静态题库中选一道题，直接注入到会话状态。

    LLM 挂了也不要紧，用户照样能做题。"""
    from code_tutor_agent.db.database import save_problem
    from code_tutor_agent.store.static_pool import get_static_problem

    topic = initial_dict.get("topic", "")
    difficulty = initial_dict.get("difficulty", "medium")

    static = get_static_problem(topic, difficulty)
    if not static:
        _generation_progress.setdefault(sid, []).append(
            "\u274c 备用题库也为空，请稍后重试或从 LeetCode 导入题目"
        )
        logger.error("Static pool exhausted — no fallback available for %s", sid)
        return

    try:
        problem_id = save_problem(static)
        meta = ProblemMeta(
            problem_id=problem_id,
            title=static["title"],
            topic=static.get("topic", topic),
            difficulty=static.get("difficulty", difficulty),
            description=static.get("description", ""),
            starter_code=static.get("starter_code", ""),
            visible_test_cases=static.get("test_cases", [])[:3],
            tag_primary="array_basics",
            prob_elo=1200,
        )

        state = SessionState(
            session_id=sid,
            problem=meta,
            status="awaiting_submit",
            topic=meta.topic,
            difficulty=meta.difficulty,
            tutor_messages=[TutorMsg(role="tutor", content=f"从备用题库选取 **{meta.title}**，加油~")],
        )
        graph = get_graph()
        await asyncio.to_thread(graph.invoke, state.model_dump(), config)
        _generation_progress.setdefault(sid, []).append(
            f"\u2705 已从备用题库选取 **{meta.title}**（{meta.difficulty}）"
        )
        logger.info("Static fallback loaded problem '%s' for session %s", static["title"], sid)

    except Exception as exc:
        logger.exception("Static fallback also failed for %s", sid)
        _generation_progress.setdefault(sid, []).append(
            f"\u274c 备用题库加载也失败了，请联系老师: {_safe_err_msg(exc)}"
        )


async def _run_suite_safe(problem_id: int, sid: str):
    """后台用 build_suite 生成完整测试套件（随机 + 边界 + 交叉验证）。

    统一入口：generation 包不再自调度（线程内无事件循环会静默跳过，且与
    API 层调度双跑），graph.invoke 返回后一律由这里调度（2026-08-10）。
    """
    try:
        # 关键：整段用例生成是纯同步阻塞（subprocess 跑参考解验证 + LLM 生成边界用例），
        # 必须丢进线程池，否则会独占事件循环，导致 SSE 的 done 事件要等全部用例生成完
        # 才能推送、题目迟迟不显示。to_thread 释放事件循环，题目就绪即可推。
        await asyncio.to_thread(build_suite, _SUITE_AGENT, problem_id, _ProgressSink(sid))
    except Exception as exc:
        logger.warning("Background suite generation failed for %d: %s", problem_id, exc)
        _generation_progress.setdefault(sid, []).append("\u26a0\ufe0f 部分测试用例生成失败")


def run_fast_path(sid: str, body: dict, graph, config):
    """Handle LeetCode fast-path — create session from parsed data, skip graph generation."""
    from code_tutor_agent.db.database import save_problem
    from code_tutor_agent.leetcode.leetcode_fetcher import extract_function_signature
    from code_tutor_agent.schemas.state import Message as TutorMsg
    from code_tutor_agent.schemas.state import ProblemMeta

    le_data = body.get("leetcode", {})
    parsed_tcs = le_data.get("parsed_test_cases") or []

    _generation_progress[sid] = ["📥 正在导入 LeetCode 题目..."]

    starter_code = le_data.get("starter_code", "")
    func_sig = extract_function_signature(starter_code)

    visible_tcs = [
        {"input_args": tc.get("input_args", []), "expected_output": tc.get("expected_output", ""), "explanation": tc.get("explanation", "")}
        for tc in parsed_tcs
    ]

    problem_dict = {
        "title": le_data.get("title", ""),
        "topic": le_data.get("topic", body.get("topic", "")),
        "difficulty": le_data.get("difficulty", body.get("difficulty", "medium")),
        "description": le_data.get("description", ""),
        "description_html": le_data.get("description_html", ""),
        "starter_code": le_data.get("starter_code", ""),
        "brute_solution": "",
        "function_signature": func_sig,
        "test_cases": parsed_tcs,
    }
    problem_id = save_problem(problem_dict)

    # 启动后台任务：调用 LLM 为该 LeetCode 题目生成最优解代码
    async def _optimal_then_tests():
        try:
            await _generate_optimal_for_leetcode_async(problem_id, sid)
        except Exception:
            pass
        try:
            await _run_suite_safe(problem_id, sid)
        except Exception:
            logger.warning("Background test generation failed for problem %d", problem_id)

    try:
        asyncio.create_task(_optimal_then_tests())
    except Exception:
        logger.warning("Failed to schedule optimal solution generation (non-blocking)")

    meta = ProblemMeta(
        problem_id=problem_id,
        title=problem_dict["title"],
        topic=problem_dict.get("topic", ""),
        difficulty=problem_dict.get("difficulty", "medium"),
        description=problem_dict.get("description", ""),
        starter_code=problem_dict.get("starter_code", ""),
        visible_test_cases=visible_tcs,
        description_html=le_data.get("description_html", ""),
        tag_primary="array_basics",
        prob_elo=1200,
    )

    initial = SessionState(
        session_id=sid,
        problem=meta,
        status="awaiting_submit",
        topic=meta.topic,
        difficulty=meta.difficulty,
        tutor_messages=[TutorMsg(role="tutor", content=f"从 LeetCode 导入 **{meta.title}**！编辑器里已填入模板代码。")],
    )
    graph.invoke(initial.model_dump(), config)
    state = graph.get_state(config)
    from code_tutor_agent.api.serializers import serialize_state
    return serialize_state(state.values)
