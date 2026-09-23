"""后台生成服务：执行 graph.invoke + 随机测试用例 + LLM 边界用例生成。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

from code_tutor_agent.api.deps import get_graph, invoke_graph_tracked
from code_tutor_agent.generation import ProblemGenerationAgent
from code_tutor_agent.generation.state import GenEvent
from code_tutor_agent.generation.suite import build_suite
from code_tutor_agent.observability import build_run_config
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

# 题目生成超时（秒），可通过环境变量覆盖
GENERATION_TIMEOUT = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120"))

# 对话出题链（services/generation.py::run_chat_generation）的 invoke 上限：
# 比 run_generation 更宽松（planner + generator + failover 重试更长）。
CHAT_GENERATION_TIMEOUT = int(
    os.getenv("CTA_CHAT_GEN_TIMEOUT", str(max(240, GENERATION_TIMEOUT * 2)))
)

# SSE 进度流（session.py::stream_progress）的「停顿超时」：连续这么久没有任何
# 新进度 / 状态推进才判定生成卡死。必须**大于** CHAT_GENERATION_TIMEOUT，
# 这样「LLM 挂住」会先由 CHAT_GENERATION_TIMEOUT 触发静态题库兜底（期间有进度
# 消息、流被续期），而不是让前端先弹出错误卡片。2026-09-22 修。
SSE_STALL_TIMEOUT = float(
    os.getenv("CTA_SSE_STALL_SECONDS", str(CHAT_GENERATION_TIMEOUT + 60))
)

# SSE 进度流的绝对上限（防止异常情况下连接永挂）
SSE_HARD_TIMEOUT = float(os.getenv("CTA_SSE_HARD_SECONDS", "900"))

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
    """create-session 快路径：graph invoke in background with timeout, then suite。

    失败（超时 / LLM 调用整体抛错）统一交给 `_fallback_problem` 走降级链兜底，
    前端通过 progress_messages 看到完整过程。
    """
    graph = get_graph()
    config = build_run_config(
        sid,
        topic=initial_dict.get("topic"),
        difficulty=initial_dict.get("difficulty"),
        user_id=initial_dict.get("user_id"),
        run_name="generate_problem",
    )

    try:
        initial = SessionState(**initial_dict)
        _emit(sid, "\U0001f680 开始生成题目...")

        # 隔离命名空间：超时兜底后孤儿线程只写 scratch，绝不污染真实会话 checkpoint
        # （方案 A：2026-09-23 修——os 线程无法被 wait_for 取消，旧逻辑会覆盖降级题）
        scratch_config, scratch_id = _scratch_config(config, sid)
        final_state = await asyncio.wait_for(
            asyncio.to_thread(invoke_graph_tracked, graph, initial.model_dump(), scratch_config, "generation"),
            timeout=GENERATION_TIMEOUT,
        )
        # 成功：把隔离命名空间的终态镜像回真实会话（孤儿线程已结束，无竞态）
        graph.update_state(config, final_state, as_node="generator_node")
        _delete_scratch(graph, scratch_id)
        _emit(sid, "\u2705 题目已就绪，正在后台生成完整测试用例...")
        await _schedule_suite(graph, config, sid)

    except asyncio.TimeoutError:
        logger.error("Generation timed out for %s after %ds → fallback chain", sid, GENERATION_TIMEOUT)
        _emit(sid, f"\u23f0 生成超时（{GENERATION_TIMEOUT}秒），LLM 响应太慢，正在按降级链选题...")
        await _fallback_problem(sid, config, initial_dict)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Background generation failed for %s → fallback chain", sid)
        _emit(sid, f"\u274c LLM 生成失败（{_safe_err_msg(exc)}），正在按降级链选题...")
        await _fallback_problem(sid, config, initial_dict)
        _delete_scratch(graph, scratch_id)  # 线程已结束（抛异常），可安全清理


def _emit(sid: str, msg: str) -> None:
    """追加一条会话进度消息（SSE /progress/stream 据此推给前端）。"""
    _generation_progress.setdefault(sid, []).append(msg)


def _scratch_config(config: dict, sid: str) -> tuple[dict, str]:
    """为一次性 graph.invoke 生成隔离的 scratch thread_id。

    根因（2026-09-23）：``asyncio.to_thread`` 跑的 OS 线程无法被 ``wait_for``
    取消；超时兜底后该线程仍会把 LLM 题 ``update_state`` 进**同一个** thread_id，
    覆盖降级链注入的题目。改用独立 scratch 命名空间后：
      - 成功：把 scratch 终态镜像回真实会话；
      - 超时/失败：孤儿只写 scratch，永不污染真实会话 checkpoint。
    """
    scratch_id = f"{sid}:scratch:{uuid.uuid4().hex}"
    scratch_config = {
        **config,
        "configurable": {**config.get("configurable", {}), "thread_id": scratch_id},
    }
    return scratch_config, scratch_id


def _delete_scratch(graph, scratch_id: str) -> None:
    """成功路径镜像完成后清理 scratch 命名空间（孤儿线程已结束，无竞态）。"""
    try:
        cp = getattr(graph, "checkpointer", None)
        if cp is not None and hasattr(cp, "delete_thread"):
            cp.delete_thread(scratch_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scratch thread cleanup failed (ignored): %s", exc)


def _problem_id_of(values: dict) -> tuple[int | None, bool]:
    """从 state values 里取 (problem_id, reused)。"""
    problem = values.get("problem")
    if not problem:
        return None, False
    pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
    reused = problem.reused if hasattr(problem, "reused") else (
        problem.get("reused") if isinstance(problem, dict) else False
    )
    return pid, bool(reused)


def _as_session_state(sid: str, values: dict) -> SessionState:
    """把 checkpointer 的 state values 还原成 SessionState（字段白名单，缺字段走默认）。"""
    fields = {k: v for k, v in (values or {}).items() if k in SessionState.model_fields}
    fields["session_id"] = sid
    try:
        return SessionState(**fields)
    except Exception:  # noqa: BLE001
        return SessionState(session_id=sid)


async def _schedule_suite(graph, config: dict, sid: str, *, await_suite: bool = False) -> None:
    """题目就绪后调度「随机 + 边界」完整用例生成；复用题/无 pid 则跳过。"""
    try:
        pid, reused = _problem_id_of(graph.get_state(config).values)
        if pid and not reused:
            logger.info("为 pid=%d 后台生成完整测试用例 (新题)", pid)
            if await_suite:
                await _run_suite_safe(pid, sid)
            else:
                asyncio.create_task(_run_suite_safe(pid, sid))
        elif reused:
            _emit(sid, "♻️ 复用已有题目，测试用例已存在，跳过测试生成")
        else:
            _emit(sid, "✅ 题目已就绪")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Background test generation failed: %s", exc)
        _emit(sid, "⚠️ 部分测试用例生成失败")


async def run_chat_generation(graph, config: dict, sid: str) -> None:
    """对话出题链的**唯一入口**（chat 路由只调用它，不再持有出题/兜底逻辑）。

    2026-09-23 收口重构：
      - graph.invoke 带超时上限（CHAT_GENERATION_TIMEOUT）；
      - 超时/抛异常时按 **降级链** 兜底（`_fallback_problem`：PULL → HISTORY → STATIC，
        跳过 LLM 通道），把结果注入会话状态 —— 用户拿到的仍是一道题，而不是
        干等或错误卡片；
      - 题目就绪后调度完整用例生成。
    """
    values = dict(graph.get_state(config).values)
    _emit(sid, "📝 正在规划并生成题目…")
    # 隔离命名空间：超时兜底后孤儿线程只写 scratch，绝不污染真实会话 checkpoint
    # （方案 A：2026-09-23 修——os 线程无法被 wait_for 取消，旧逻辑会覆盖降级题）
    scratch_config, scratch_id = _scratch_config(config, sid)
    try:
        final_state = await asyncio.wait_for(
            asyncio.to_thread(
                invoke_graph_tracked, graph, values, scratch_config, "leetcode_generation"
            ),
            timeout=CHAT_GENERATION_TIMEOUT,
        )
        # 成功：把隔离命名空间的终态镜像回真实会话（孤儿线程已结束，无竞态）
        graph.update_state(config, final_state, as_node="generator_node")
        _delete_scratch(graph, scratch_id)
        _emit(sid, "✅ 题目已就绪，正在后台生成完整测试用例...")
    except asyncio.TimeoutError:
        logger.error(
            "Chat-driven generation timed out for %s after %ss → fallback chain",
            sid, CHAT_GENERATION_TIMEOUT,
        )
        _emit(sid, f"⏳ 生成超时（{CHAT_GENERATION_TIMEOUT}秒），正在按降级链选题...")
        await _fallback_problem(sid, config, values)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Chat-driven generation failed for %s → fallback chain", sid)
        _emit(sid, f"❌ 生成失败（{_safe_err_msg(exc)}），正在按降级链选题...")
        await _fallback_problem(sid, config, values)
        _delete_scratch(graph, scratch_id)  # 线程已结束（抛异常），可安全清理
        return

    await _schedule_suite(graph, config, sid, await_suite=True)


def _safe_err_msg(exc: Exception) -> str:
    """截取异常信息的前 80 个字符，避免在 UI 展示过长的调用栈。"""
    msg = str(exc)
    return msg[:80] + "..." if len(msg) > 80 else msg


async def _fallback_problem(sid: str, config: dict, values: dict) -> None:
    """带外兜底唯一入口：跳过 LLM 通道，按降级链取题并注入会话状态。

    与节点内 `ProblemGenerationAgent.run` 用的是**同一条** `_FALLBACK_CHAIN`
    （leetcode_pull → db_unac → static），不再单独复刻「直接取静态题」的逻辑，
    因此链上任何一档成功都算成功；只有整条链都空才算失败。

    调用点（2 处，均在图外）：
      - `run_chat_generation`：对话出题 invoke 被掐断 / 超时；
      - `run_generation`：create-session 快路径生成整体失败。
    """
    from code_tutor_agent.generation.state import GenerationContext
    from code_tutor_agent.nodes.generator import _profile_hint_from, _translate_to_command

    topic = values.get("topic") or ""
    difficulty = values.get("difficulty") or "medium"
    # 排除集合：当前会话已出现/已做过的题，兜底也不给重复题
    exclude: set[int] = set()
    cur_pid, _ = _problem_id_of(values)
    if cur_pid:
        exclude.add(cur_pid)
    for sub in (values.get("submissions") or []):
        spid = getattr(sub, "problem_id", None)
        if spid is None and isinstance(sub, dict):
            spid = sub.get("problem_id")
        if spid:
            exclude.add(spid)

    state = _as_session_state(sid, values)
    try:
        profile_hint = _profile_hint_from(state)
    except Exception:  # noqa: BLE001
        profile_hint = None

    ctx = GenerationContext(
        topic=topic or "数组",
        difficulty=difficulty,
        profile_hint=profile_hint,
        exclude_problem_ids=exclude,
    )
    result = None
    try:
        # 注意 force_fallback 是 keyword-only：必须用关键字传参
        result = await asyncio.to_thread(
            _SUITE_AGENT.run, ctx, _ProgressSink(sid), force_fallback=True
        )
    except Exception:  # noqa: BLE001
        logger.exception("Fallback chain crashed for %s", sid)

    if result is None or not result.ok or not result.draft:
        logger.error(
            "Fallback chain exhausted for %s (chain=%s)",
            sid, getattr(result, "fallback_chain", None),
        )
        _emit(sid, "❌ 降级链也没能选出题目，请稍后重试或换一道题练习")
        return

    try:
        # 注入交给节点内权威实现（ProblemMeta/欢迎语/status/phase 一次性对齐）
        cmd = _translate_to_command(state, result)
        get_graph().update_state(config, getattr(cmd, "update", None) or {}, as_node="generator_node")
        _emit(sid, f"✅ 已按降级链选取 **{result.draft.title}**（通道 {result.channel}）")
        logger.info(
            "Fallback chain loaded problem %s via %s for session %s",
            result.problem_id, result.channel, sid,
        )
        if result.problem_id and not result.reused:
            await _run_suite_safe(result.problem_id, sid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallback chain injection failed for %s", sid)
        _emit(sid, f"❌ 兜底题目注入失败，请联系老师: {_safe_err_msg(exc)}")


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

