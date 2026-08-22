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
                reused = problem.reused if hasattr(problem, "reused") else (problem.get("reused") if isinstance(problem, dict) else False)
                if pid and not reused:
                    # 彻底独立于 run_generation 生命周期：题目就绪后即可进做题，
                    # 复杂测试用例在独立 task 中后台生成（不阻塞本协程返回）。
                    # 复用已有题目（reused=True）时跳过：其测试用例已生成，重跑会覆盖。
                    logger.info("为 pid=%d 后台生成完整测试用例 (新题)", pid)
                    asyncio.create_task(_run_suite_safe(pid, sid))
                else:
                    logger.info("pid=%s 为复用题/无 pid，跳过测试生成", pid)
                    _generation_progress.setdefault(sid, []).append(
                        "♻️ 复用已有题目，测试用例已存在，跳过测试生成"
                        if reused else
                        "✅ 题目已就绪"
                    )
            else:
                _generation_progress.setdefault(sid, []).append("✅ 题目已就绪")
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
        problem_id, _reused = save_problem(static)
        meta = ProblemMeta(
            problem_id=problem_id,
            title=static["title"],
            topic=static.get("topic", topic),
            difficulty=static.get("difficulty", difficulty),
            description=static.get("description", ""),
            starter_code=static.get("starter_code", ""),
            visible_test_cases=static.get("test_cases", [])[:3],
            constraints=static.get("constraints") or [],
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

