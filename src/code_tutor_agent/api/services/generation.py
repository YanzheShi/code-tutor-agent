"""后台生成服务：执行 graph.invoke + 随机测试用例 + LLM 边界用例生成。"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)


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
    """Full graph invoke in background, then generate complex tests."""
    graph = get_graph()
    config = {"configurable": {"thread_id": sid}}

    try:
        initial = SessionState(**initial_dict)
        _generation_progress.setdefault(sid, []).append("\U0001f680 开始生成题目...")

        await asyncio.to_thread(graph.invoke, initial.model_dump(), config)
        _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪，正在后台生成完整测试用例...")

        try:
            state = graph.get_state(config)
            problem = state.values.get("problem")
            if problem:
                pid = problem.problem_id if hasattr(problem, "problem_id") else problem.get("problem_id")
                if pid:
                    # Always trigger background test generation; _generate_complex_tests
                    # reads optimal_solution / function_signature from the DB directly,
                    # so it works regardless of whether the problem came from LeetCode
                    # or from LLM generation.
                    await _generate_complex_tests(pid, sid)
                else:
                    _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪")
            else:
                _generation_progress.setdefault(sid, []).append("\u2705 题目已就绪")
        except Exception as exc:
            logger.warning("Background test generation failed: %s", exc)
            _generation_progress.setdefault(sid, []).append("\u26a0\ufe0f 部分测试用例生成失败")

    except Exception as exc:
        logger.exception("Background generation failed for %s", sid)
        _generation_progress.setdefault(sid, []).append(f"\u274c 生成失败: {exc}")


async def _generate_complex_tests(problem_id: int, sid: str):
    """Generate random + boundary test cases in background."""
    from code_tutor_agent.db.database import get_problem_by_id, update_problem_test_cases
    from code_tutor_agent.sandbox.input_generator import generate_random_inputs
    from code_tutor_agent.sandbox.runner import run_solution

    logger.info("_generate_complex_tests() — problem_id=%d", problem_id)
    full = get_problem_by_id(problem_id)
    if not full:
        logger.warning("Problem %d not found", problem_id)
        return

    # 优先尝试 optimal_solution，再降级到 brute_solution（向后兼容）
    brute_code = full.optimal_solution or full.brute_solution
    func_sig = full.function_signature
    if not brute_code:
        logger.warning("No optimal_solution or brute_solution for %d — skipping bg test gen", problem_id)
        _generation_progress.setdefault(sid, []).append("📝 无参考代码，跳过后台测试生成")
        return

    _generation_progress.setdefault(sid, []).append("\U0001f9ea 正在生成更多测试用例...")

    random_inputs = generate_random_inputs(func_sig, count=12, seed=problem_id)
    logger.info("Generated %d random inputs", len(random_inputs))
    if not random_inputs:
        _generation_progress.setdefault(sid, []).append("✅ 无函数签名，跳过随机测试生成")
        return

    _generation_progress.setdefault(sid, []).append(f"\U0001f527 正在运行暴力解验证 {len(random_inputs)} 个用例...")
    all_tcs: list[dict] = []
    for idx, inp in enumerate(random_inputs):
        tc = {
            "input_args": inp,
            "expected_output": "",
            "is_hidden": idx >= 4,
            "explanation": f"随机生成测试 {idx+1}",
        }
        results = run_solution(brute_code, [tc], timeout=10.0)
        if results:
            r = results[0]
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
        constraints_str = ""  # constraints not stored in DB — see models.py

        prompt_user = GENERATE_BOUNDARY_USER.format(
            title=full.title,
            description=full.description,
            difficulty=full.difficulty,
            function_signature=func_sig,
            constraints=constraints_str,
            brute_code=brute_code,
            existing_cases=existing_cases_str,
            count=8,
        )

        llm = get_llm("agnes", temperature=0.5)
        resp = llm.invoke([
            ("system", GENERATE_BOUNDARY_SYSTEM),
            ("human", prompt_user),
        ])
        content = resp.content if hasattr(resp, "content") else str(resp)

        json_match = re.search(r"\[.*?\]", content, re.DOTALL)
        if json_match:
            boundary_cases = json.loads(json_match.group(0))
            logger.info("LLM generated %d boundary cases", len(boundary_cases))

            _generation_progress.setdefault(sid, []).append(f"\U0001f527 正在验证 {len(boundary_cases)} 个边界用例...")
            for bc in boundary_cases:
                results = run_solution(brute_code, [{
                    "input_args": bc.get("input_args", []),
                    "expected_output": bc.get("expected_output", ""),
                }], timeout=10.0)
                if results and results[0].detail:
                    bc["expected_output"] = results[0].detail
                    bc["is_hidden"] = True
                    bc["explanation"] = bc.get("explanation", "LLM 生成的边界用例")
                    all_tcs.append(bc)
                else:
                    logger.warning("Boundary case validation failed: %s", bc.get("explanation", ""))
    except Exception as exc:
        logger.warning("Prompt B (boundary LLM) failed: %s", exc)

    existing_tcs = full.test_cases
    sample_tcs = [tc for tc in existing_tcs if not tc.get("is_hidden", False)][:2]
    full_suite = sample_tcs + all_tcs

    update_problem_test_cases(problem_id, full_suite)
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
            await _generate_complex_tests(problem_id, sid)
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
