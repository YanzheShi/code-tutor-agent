"""生成器节点 — Day2 简化流程。

**流程图（代码注释中确保可追溯）：**

    START → planner_node → generator_node → wait_for_submit_node
                                                     │
                                            [用户写代码]
                                                     │
                                                     ▼
                                               judge_node（使用全量测试用例）
                                                     │
                                                     ▼
                                               tutor_node → planner / wait

**生成器节点内部流程：**

    1. LLM 生成：题目描述 + optimal_solution + starter_code + function_signature
           （不生成测试用例和 brute_solution — 这些后续本地生成）
    2. 解析 function_signature → 确定参数类型
    3. 通过 Python random 生成 2 组随机输入
    4. 用 optimal_solution 跑这 2 组输入 → 得到期望输出
    5. 这 2 组（输入, 期望输出）对 → 示例测试用例（用户可见）
    6. 将题目连同示例用例保存到 DB
    7. 返回状态 status="awaiting_submit"

    → 后台（graph 返回后，由 API 层执行）：
    8. 生成 10+ 组随机输入，运行参考解 → 基础测试用例
    9. LLM 生成边界测试用例
    10. 合并两者 → 更新 DB 中的全量测试套件
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from code_tutor_agent.agents.problem_generator import generate_problem
from code_tutor_agent.db.database import save_problem
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import Message as TutorMsg
from code_tutor_agent.schemas.state import ProblemMeta, SessionState
from code_tutor_agent.store.static_pool import get_static_problem

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
MODEL_ALIAS = "agnes"


def _progress(sid: str, msg: str):
    """Write a progress message for a session (thread-safe shared dict)."""
    _generation_progress.setdefault(sid, []).append(msg)


def _build_test_case(input_args: list[str], expected_output: str, explanation: str = "") -> dict:
    """Build a test case dict in the format expected by the DB."""
    return {
        "input_args": input_args,
        "expected_output": expected_output,
        "is_hidden": False,
        "explanation": explanation,
    }


def _generate_from_leetcode(
    sid: str,
    lc_data: dict[str, Any],
    progress_cb: Callable[[str], None],
) -> Command:
    """Build a problem directly from parsed LeetCode data.

    This path skips the LLM entirely — the problem description,
    examples, tags, and starter code all come from the LeetCode API.
    """
    title = lc_data.get("title", "LeetCode Problem")
    description = lc_data.get("description", "")
    difficulty = lc_data.get("difficulty", "medium")
    examples = lc_data.get("examples", [])
    starter_code = lc_data.get("starter_code", "")
    tags = lc_data.get("tags", [])
    hints = lc_data.get("hints", [])

    # Derive a topic from the first tag, or fall back to "算法"
    topic = tags[0] if tags else "算法"

    # Build visible test cases from examples
    from code_tutor_agent.leetcode.leetcode_fetcher import _parse_examples_to_test_cases
    visible_tcs = _parse_examples_to_test_cases(examples, "")
    logger.info("Parsed %d visible test cases from LeetCode examples", len(visible_tcs))

    # We don't have optimal_solution from LeetCode, so save a placeholder.
    # The background test generation will need to derive expected outputs
    # differently — but for now the problem is still usable (user can run
    # and submit; the full test suite generation will be skipped if no optimal).
    problem_dict = {
        "title": title,
        "topic": topic,
        "difficulty": difficulty,
        "description": description,
        "starter_code": starter_code,
        "test_cases": visible_tcs,
        "novelty_score": 9.0,  # LeetCode problems are inherently novel
        "brute_solution": "",  # No brute force — skip bg test gen
        "function_signature": "",
    }

    # Save to DB (returns problem_id)
    problem_id = save_problem(problem_dict)

    meta = ProblemMeta(
        problem_id=problem_id,
        title=title,
        topic=topic,
        difficulty=difficulty,
        description=description,
        description_html=lc_data.get("description_html", description),
        starter_code=starter_code,
        visible_test_cases=visible_tcs,
        novelty_score=9.0,
    )

    welcome_msg = TutorMsg(
        role="tutor",
        content=f"来自 LeetCode 的 **{title}** 🎯  \n\n"
                f"难度: {difficulty} | 标签: {', '.join(tags)}\n\n"
                f"编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。",
    )

    progress_cb("✅ 题目已就绪！")

    return Command(
        update={
            "problem": meta,
            "status": "awaiting_submit",
            "submissions": [],
            "hint_level": 0,
            "tutor_messages": [welcome_msg],
            "last_verdict": None,
            "adversarial_triggered": False,
            "error_message": "",
            "leetcode": None,  # Clear so it's not reprocessed
            # These are needed by the API layer's background test gen
            "_brute_code": "",
            "_function_signature": "",
            "_problem_id": problem_id,
        },
        goto="wait_for_submit_node",
    )


def generator_node(state: SessionState, progress_cb: Callable[[str], None] | None = None) -> Command:
    """Day2 generator: LLM → problem+brute → random 2 sample I/O → deliver.

    Graph flow (see module docstring):
        planner_node → generator_node [HERE] → wait_for_submit_node

    When state.leetcode is set (user pasted a LeetCode URL), skip LLM
    generation entirely and build the problem directly from the parsed data.

    The full test suite is generated in the background by the API layer
    after this node returns, while the user is writing code.
    """
    logger.info("▶ generator_node() — topic=%s, difficulty=%s", state.topic, state.difficulty)
    sid = state.session_id
    if progress_cb is None:
        progress_cb = lambda msg: None

    topic = state.topic
    difficulty = state.difficulty

    # ── 路径 A：LeetCode 导入（跳过 LLM 生成）──
    lc_data = state.leetcode
    if lc_data:
        logger.info("LeetCode data detected — skipping LLM generation")
        _progress(sid, "📥 使用 LeetCode 题目…")
        return _generate_from_leetcode(sid, lc_data, progress_cb)

    # ── 路径 B：正常 LLM 生成（现有流程）──
    problem_dict: dict[str, Any] | None = None
    problem_obj: Problem | None = None

    _progress(sid, "正在调用大模型生成题目…")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("Attempt %d/%d — LLM generate problem + brute code", attempt, MAX_ATTEMPTS)
        _progress(sid, f"第 {attempt}/{MAX_ATTEMPTS} 次尝试 — 生成中…")

        try:
            problem_obj = generate_problem(topic=topic, difficulty=difficulty)
            problem_dict = problem_obj.model_dump()
        except Exception as exc:
            logger.warning("LLM generation failed: %s", exc)
            _progress(sid, f"⚠️ LLM 调用失败，重试中…")
            continue

        brute_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "")
        if not brute_code:
            logger.warning("No optimal_solution in output — retrying")
            continue

        # ── Step 2: Parse LLM examples into test cases ──
        examples = problem_dict.get("examples", [])
        func_sig = problem_dict.get("function_signature", "")
        logger.info("Parsing %d examples, sig=%s", len(examples), func_sig)

        _progress(sid, "🧪 正在解析示例测试用例…")

        from code_tutor_agent.leetcode.leetcode_fetcher import _parse_examples_to_test_cases
        sample_tcs = _parse_examples_to_test_cases(examples, func_sig)

        if not sample_tcs:
            logger.warning("Failed to parse examples into test cases — retrying")
            _progress(sid, "⚠️ 示例解析失败，重新生成…")
            continue

        # ── Step 3: Run optimal_solution on examples → get expected outputs ──
        all_ok = True
        for tc in sample_tcs:
            input_args = tc.get("input_args", [])
            expected = tc.get("expected_output", "")
            if expected and expected not in ("", "..."):
                continue
            results = run_solution(brute_code, [tc], timeout=10.0)
            if results:
                r = results[0]
                actual = r.detail or ""
                if actual:
                    tc["expected_output"] = actual
                    logger.debug("TC %s -> %s", input_args, actual)
                else:
                    logger.warning("TC %s: no actual output (%s)", input_args, r.status)
                    all_ok = False
            else:
                logger.warning("TC %s: no results", input_args)
                all_ok = False

        if not all_ok or not sample_tcs:
            logger.warning("Sample generation failed — retrying")
            _progress(sid, "⚠️ 示例生成失败，重新生成题目…")
            continue

        # ── All checks passed for the lightweight generation ──
        logger.info("Lightweight generation OK — %d sample test cases", len(sample_tcs))
        _progress(sid, "✅ 题目已就绪！")
        break
    else:
        # ── Fallback: static pool ──
        logger.warning("All %d attempts failed — falling back to static pool", MAX_ATTEMPTS)
        _progress(sid, "⚠️ 出题超限，切换到静态题库…")
        problem_dict = get_static_problem(topic=topic, difficulty=difficulty)
        if problem_dict is None:
            problem_dict = get_static_problem()
        logger.info("Fallback → %s", problem_dict.get("title", "unknown"))
        sample_tcs = problem_dict.get("test_cases", [])[:2]
        sample_tcs = [tc for tc in sample_tcs if not tc.get("is_hidden", False)][:2]

    # ── 持久化到 DB — 先保存示例用例，完整用例后续补充 ──
    # The full test_suite will be generated in the background (API layer)
    # and saved via update_problem_test_cases()
    if problem_dict:
        problem_dict["test_cases"] = sample_tcs
    problem_id = save_problem(problem_dict or {})

    # If dedup happened (title already existed), reload from DB for correct starter_code
    from code_tutor_agent.db.database import get_problem_by_id
    db_problem = get_problem_by_id(problem_id)
    db_starter_code = db_problem.get("starter_code", "") if db_problem else ""
    db_optimal = db_problem.get("optimal_solution", "") if db_problem else ""

    # Build visible test cases (sample ones)
    visible_tcs = [
        {
            "input_args": tc.get("input_args", []),
            "expected_output": tc.get("expected_output", ""),
            "explanation": tc.get("explanation", ""),
        }
        for tc in sample_tcs
    ]

    meta = ProblemMeta(
        problem_id=problem_id,
        title=problem_dict.get("title", "Unknown") if problem_dict else "Unknown",
        topic=problem_dict.get("topic", topic) if problem_dict else topic,
        difficulty=problem_dict.get("difficulty", difficulty) if problem_dict else difficulty,
        description=problem_dict.get("description", "") if problem_dict else "",
        description_html=problem_dict.get("description", "") if problem_dict else "",
        starter_code=db_starter_code or (problem_dict.get("starter_code", "") if problem_dict else ""),
        visible_test_cases=visible_tcs,
        novelty_score=problem_dict.get("novelty_score", 7.0) if problem_dict else 7.0,
    )

    # Store brute_code + function_signature in state for background test generation
    brute_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "") if problem_dict else ""
    func_sig = problem_dict.get("function_signature", "") if problem_dict else ""

    welcome_msg = TutorMsg(
        role="tutor",
        content=f"来，试试这道 **{meta.title}**！\n\n编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。",
    )

    _progress(sid, "✅ 题目已就绪！")

    # ── 返回 state — graph 路由到 wait_for_submit_node ──
    # The background test generation is triggered by the API layer
    # after _graph.invoke() returns (see _run_generation in api/main.py)
    update = {
        "problem": meta,
        "status": "awaiting_submit",
        "submissions": [],
        "hint_level": 0,
        "tutor_messages": [welcome_msg],
        "last_verdict": None,
        "adversarial_triggered": False,
        "error_message": "",
        # Store for background test generation (API layer reads these)
        "_brute_code": brute_code,
        "_function_signature": func_sig,
        "_problem_id": problem_id,
    }

    return Command(update=update, goto="wait_for_submit_node")