"""后台生成服务：执行 graph.invoke + 随机测试用例 + LLM 边界用例生成。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.state import ProblemMeta, Message as TutorMsg, SessionState

logger = logging.getLogger(__name__)

# 参考解在这些状态下说明 input 本身有问题（或参考解崩了），该用例应丢弃
_DROP_STATUSES = {"Runtime Error", "TLE", "Judge Error"}

# 题目生成超时（秒），可通过环境变量覆盖
GENERATION_TIMEOUT = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120"))


def _extract_code_from_llm_response(text: str) -> str:
    """Extract Python code from LLM response — strip markdown fences if present."""
    import re
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
        llm = get_llm("agnes", temperature=0.3)
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
    config = {"configurable": {"thread_id": sid}}

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
                    asyncio.create_task(_run_complex_tests_safe(pid, sid))
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


async def _run_complex_tests_safe(problem_id: int, sid: str):
    """Wrapper that swallows exceptions from background test generation.

    Prevents ``asyncio.create_task`` "Task exception was never retrieved"
    warnings and keeps a dangling reference to the failure in the UI.
    """
    try:
        # 关键：整段用例生成是纯同步阻塞（subprocess 跑参考解验证 + LLM 生成边界用例），
        # 必须丢进线程池，否则会独占事件循环，导致 SSE 的 done 事件要等全部用例生成完
        # 才能推送、题目迟迟不显示。to_thread 释放事件循环，题目就绪即可推。
        await asyncio.to_thread(_generate_complex_tests, problem_id, sid)
    except Exception as exc:
        logger.warning("Background complex test generation failed for %d: %s", problem_id, exc)
        _generation_progress.setdefault(sid, []).append("\u26a0\ufe0f 部分测试用例生成失败")


def _generate_complex_tests(problem_id: int, sid: str):
    """Generate random + boundary test cases (SYNCHRONOUS).

    整段是纯同步阻塞：subprocess 跑参考解做验证 + LLM 生成边界用例。
    一律由调用方用 ``asyncio.to_thread`` 丢进线程池执行，切勿直接 await，
    否则会独占事件循环，导致 SSE 的 done 事件要等全部用例生成完才推送、
    题目迟迟不显示。
    """
    from code_tutor_agent.db.database import get_problem_by_id, update_problem_test_cases
    from code_tutor_agent.sandbox.input_generator import (
        generate_random_inputs,
        sanitize_test_case,
        _needs_sorted_inputs,
    )
    from code_tutor_agent.sandbox.runner import run_solution

    logger.info("_generate_complex_tests() — problem_id=%d", problem_id)
    full = get_problem_by_id(problem_id)
    if not full:
        logger.warning("Problem %d not found", problem_id)
        return

    # 优先尝试 optimal_solution，再降级到 brute_solution（向后兼容）
    brute_code = full.optimal_solution or full.brute_solution
    func_sig = full.function_signature
    # 「有序」类题目（合并有序数组 / 有序数组二分等）：对输入数组排序（方案 B）。
    sort_inputs = _needs_sorted_inputs(*(getattr(full, "constraints", None) or []), getattr(full, "description", None) or "")
    if not brute_code:
        logger.warning("No optimal_solution or brute_solution for %d — skipping bg test gen", problem_id)
        _generation_progress.setdefault(sid, []).append("📝 无参考代码，跳过后台测试生成")
        return

    _generation_progress.setdefault(sid, []).append("\U0001f9ea 正在生成更多测试用例...")

    random_inputs = generate_random_inputs(
        func_sig, count=12, seed=problem_id,
        constraints=getattr(full, "constraints", None),
        description=getattr(full, "description", None),
    )
    logger.info("Generated %d random inputs", len(random_inputs))
    if not random_inputs:
        _generation_progress.setdefault(sid, []).append("✅ 无函数签名，跳过随机测试生成")
        return

    _generation_progress.setdefault(sid, []).append(f"\U0001f527 正在运行暴力解验证 {len(random_inputs)} 个用例...")
    all_tcs: list[dict] = []
    for idx, inp in enumerate(random_inputs):
        # 先校正 input 契约（重算 m/n、补零到 m+n 等），校正失败则丢弃。
        # 不校正的话，合并类题的 nums1 没补零，参考解会越界 RE。
        if func_sig:
            san = sanitize_test_case(func_sig, {"input_args": inp}, sort_inputs=sort_inputs)
            if not san or not san.get("input_args"):
                logger.warning("Random case input malformed, dropping (pid=%d idx=%d)", problem_id, idx)
                continue
            inp = san["input_args"]
        tc = {
            "input_args": inp,
            "expected_output": "",
            "is_hidden": idx >= 4,
            "explanation": f"随机生成测试 {idx+1}",
        }
        results = run_solution(brute_code, [tc], timeout=10.0, function_signature=func_sig)
        if not results:
            continue
        r = results[0]
        if r.status in _DROP_STATUSES:  # 参考解跑挂 -> 该用例无效，丢弃
            logger.warning("Random case ref %s, dropping (pid=%d idx=%d)", r.status, problem_id, idx)
            continue
        actual = r.detail or ""
        if actual:
            tc["expected_output"] = actual
            all_tcs.append(tc)

    _generation_progress.setdefault(sid, []).append("\U0001f916 正在生成边界测试用例...")
    try:
        from code_tutor_agent.config import get_llm
        from code_tutor_agent.prompts.generate_boundary_cases import (
            GENERATE_BOUNDARY_SYSTEM,
            GENERATE_BOUNDARY_USER,
        )

        existing_cases_str = "\n".join(
            f"  #{i+1}: input_args={tc.get('input_args', [])} \u2192 {tc.get('expected_output', '')}"
            for i, tc in enumerate(all_tcs[:4])
        )
        constraints_str = "\n".join(f"  - {c}" for c in full.constraints) if full.constraints else ""

        prompt_user = GENERATE_BOUNDARY_USER.format(
            title=full.title,
            description=full.description,
            difficulty=full.difficulty,
            function_signature=func_sig,
            constraints=constraints_str,
            optimal_code=brute_code,
            existing_cases=existing_cases_str,
            count=8,
        )

        llm = get_llm("agnes", temperature=0.5)
        resp = llm.invoke([
            ("system", GENERATE_BOUNDARY_SYSTEM),
            ("human", prompt_user),
        ])
        content = resp.content if hasattr(resp, "content") else str(resp)

        # 贪婪匹配到最后一个 ]：避免 input_args 里的 "[1,2,3]" 等含方括号字符串
        # 被非贪婪 .*? 提前截断导致 JSON 解析失败
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if json_match:
            boundary_cases = json.loads(json_match.group(0))
            logger.info("LLM generated %d boundary cases", len(boundary_cases))

            _generation_progress.setdefault(sid, []).append(f"\U0001f527 正在验证 {len(boundary_cases)} 个边界用例...")
            for bc in boundary_cases:
                # 先校正 input 契约（重算 m/n、补零等），校正失败则丢弃
                if func_sig:
                    bc = sanitize_test_case(func_sig, bc, sort_inputs=sort_inputs) or {}
                    if not bc.get("input_args"):
                        logger.warning("Boundary case input malformed, dropping: %s", bc.get("explanation", ""))
                        continue
                results = run_solution(brute_code, [{
                    "input_args": bc.get("input_args", []),
                    # 强制空 expected：让 runner 进入「回传实际输出」模式，
                    # 避免 LLM 自带的 expected 触发比较模式把 detail 污染成
                    # "expected=... got=..." 报告字符串。
                    "expected_output": "",
                }], timeout=10.0, function_signature=func_sig)
                if not results:
                    logger.warning("Boundary case no result, dropping: %s", bc.get("explanation", ""))
                    continue
                r = results[0]
                if r.status in _DROP_STATUSES:  # 参考解跑挂 -> 该用例无效，丢弃
                    logger.warning("Boundary case ref %s, dropping: %s", r.status, bc.get("explanation", ""))
                    continue
                actual = r.detail or ""
                if actual:
                    bc["expected_output"] = actual
                    bc["is_hidden"] = True
                    bc["explanation"] = bc.get("explanation", "LLM 生成的边界用例")
                    all_tcs.append(bc)
                else:
                    logger.warning("Boundary case ref produced empty output, dropping: %s", bc.get("explanation", ""))
    except Exception as exc:
        logger.warning("Prompt B (boundary LLM) failed: %s", exc)

    # ── 关键修复：验证 LLM 生成的示例/可见用例 ──
    # 这些用例的 expected 是 LLM 在题目契约 Examples 里直接编的（从未验证），
    # 会出现「编错数字」类错误（如题目 15 把 [-7,-3,2,3,11] 期望写成 [4,9,16,49,121]）。
    # 用参考解重算期望并校验，参考解跑挂的用例直接丢弃。
    existing_tcs = full.test_cases
    sample_tcs = [tc for tc in existing_tcs if not tc.get("is_hidden", False)][:2]
    verified_visible: list[dict] = []
    for tc in sample_tcs:
        san = dict(tc)
        if func_sig:
            san = sanitize_test_case(func_sig, san, sort_inputs=sort_inputs) or {}
        if not san or not san.get("input_args"):
            logger.warning("Sample/visible case input malformed, dropping: %s", tc.get("explanation", ""))
            continue
        results = run_solution(
            brute_code,
            [{"input_args": san["input_args"], "expected_output": ""}],
            timeout=10.0, function_signature=func_sig,
        )
        if not results:
            logger.warning("Sample/visible case no result, dropping: %s", tc.get("explanation", ""))
            continue
        r = results[0]
        if r.status in _DROP_STATUSES:  # 参考解跑挂 -> 该示例无效，丢弃
            logger.warning("Sample/visible case ref %s, dropping: %s", r.status, tc.get("explanation", ""))
            continue
        actual = r.detail or ""
        if actual:
            san["expected_output"] = actual
            san["is_hidden"] = False
            verified_visible.append(san)
        else:
            logger.warning("Sample/visible case ref produced empty output, dropping: %s", tc.get("explanation", ""))

    full_suite = verified_visible + all_tcs
    # 可见用例 = 已验证的示例用例 + 已验证的随机用例兜底（至多 4 条），全部经参考解验证。
    visible_final = [tc for tc in full_suite if not tc.get("is_hidden", False)][:4]

    update_problem_test_cases(problem_id, full_suite, visible_final)
    _generation_progress.setdefault(sid, []).append(f"\u2705 共 {len(full_suite)} 个测试用例已就绪（含 LLM 边界用例）")
    logger.info("Completed background test gen for problem %d", problem_id)


def run_fast_path(sid: str, body: dict, graph, config):
    """Handle LeetCode fast-path — create session from parsed data, skip graph generation."""
    from code_tutor_agent.db.database import save_problem
    from code_tutor_agent.schemas.state import ProblemMeta, Message as TutorMsg
    from code_tutor_agent.leetcode.leetcode_fetcher import extract_function_signature

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
            await asyncio.to_thread(_generate_complex_tests, problem_id, sid)
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
