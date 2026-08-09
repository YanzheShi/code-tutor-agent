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
import re
from typing import Any, Literal

from langgraph.config import get_stream_writer
from langgraph.types import Command

from code_tutor_agent.agents.agent_problem import ProblemAgent, ProblemChannel
from code_tutor_agent.db.database import (
    get_problem_by_id,
    save_problem,
    update_problem_optimal_solution,
)
from code_tutor_agent.leetcode.leetcode_fetcher import (
    _parse_examples_to_test_cases,
    extract_function_signature,
)
from code_tutor_agent.progress import _generation_progress
from code_tutor_agent.sandbox.ds import get_struct_prologue
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import (
    Message as TutorMsg,
    ProblemMeta,
    SessionPhase,
    SessionState,
)
from code_tutor_agent.store.static_pool import get_static_problem

logger = logging.getLogger(__name__)

# MODEL_ALIAS constant removed — model selection now via config.PURPOSE_CONFIGS

# ── Topic → Tag enum 映射 ──
_TOPIC_TAG_MAP: dict[str, str] = {
    "数组": "array_basics",
    "数组+哈希表": "array_basics",
    "双指针": "array_two_pointers",
    "滑动窗口": "array_sliding_window",
    "二分查找": "array_binary_search",
    "链表": "linkedlist_basics",
    "栈": "stack_basics",
    "队列": "queue_deque",
    "动态规划": "dp_1d",
    "字符串": "string_basics",
    "递归": "backtrack",
    "贪心": "greedy",
    "位运算": "bit_manip",
    "排序": "array_sorting",
    "前缀和": "array_prefix_sum",
    # ── 图 / 树 / 堆 / 并查集 等（取值须为合法 Tag 枚举，避免默认 array_basics）──
    "图": "graph_dfs",
    "图论": "graph_dfs",
    "图遍历": "graph_dfs",
    "图的dfs": "graph_dfs",
    "图的bfs": "graph_bfs",
    "图bfs": "graph_bfs",
    "拓扑排序": "graph_topo",
    "最短路径": "graph_dfs",
    "并查集": "union_find",
    "树": "tree_dfs",
    "树结构": "tree_dfs",
    "二叉树": "tree_bfs",
    "堆": "heap_priority_queue",
    "优先队列": "heap_priority_queue",
    "回溯": "backtrack",
    "数论": "math_number_theory",
}


# ── 中文知识点口语 → 规范化出题描述 ──
# 典型坑：用户说「图」被 skill/LLM 理解成「图片 / 网格矩阵」，实际指图论
# （graph theory）。这里把口语归一化为明确的出题描述后再喂给 LLM / skill。
_TOPIC_GEN_MAP: dict[str, str] = {
    "图": "图论（graph theory：顶点与边的数据结构与算法，例如图的 DFS/BFS 遍历、连通分量、最短路径、拓扑排序；注意：不是图片/图像/像素，也不是二维网格矩阵 grid）",
    "图论": "图论（graph theory：顶点与边的数据结构与算法，例如图的 DFS/BFS 遍历、连通分量、最短路径、拓扑排序）",
    "图遍历": "图论遍历（图的 DFS/BFS，邻接表/邻接矩阵表示，含 visited 集合防环）",
    "图的bfs": "图的广度优先搜索（BFS，队列实现，邻接表/邻接矩阵表示）",
    "图的dfs": "图的深度优先搜索（DFS，递归或栈，邻接表/邻接矩阵表示）",
    "拓扑排序": "拓扑排序（topological sort，有向无环图 DAG，Kahn 算法或 DFS 后序）",
    "最短路径": "最短路径（图论，Dijkstra / Bellman-Ford / Floyd）",
    "并查集": "并查集（union-find / disjoint set，路径压缩 + 按秩合并）",
    "树": "二叉树/树结构（tree，例如遍历、LCA、路径和、直径）",
    "二叉树": "二叉树（binary tree，例如前中后序遍历、层序、LCA）",
    "堆": "堆 / 优先队列（heap / priority queue，例如堆排序、Top-K、中位数）",
    "优先队列": "优先队列（priority queue，heap 实现）",
    "回溯": "回溯（backtracking，例如排列/组合/子集、N 皇后）",
    "贪心": "贪心（greedy，局部最优推导全局最优）",
    "位运算": "位运算（bit manipulation，异或、掩码、lowbit）",
    "数论": "数论（number theory，质数、GCD、模运算）",
}


def normalize_topic_for_generation(topic: str) -> str:
    """把用户口语化知识点（如中文「图」）归一化为明确的出题描述。

    优先精确匹配；否则取包含该子串的最长键（如「图的bfs」优先于「图」）；
    未命中时原样返回，避免误伤英文 slug 或标准术语（如 'two-sum'）。
    """
    if not topic:
        return topic
    t = topic.strip()
    if t in _TOPIC_GEN_MAP:
        return _TOPIC_GEN_MAP[t]
    for key in sorted(_TOPIC_GEN_MAP, key=len, reverse=True):
        if key in t:
            return _TOPIC_GEN_MAP[key]
    return t


def tag_for(topic: str) -> str:
    """Map a Chinese topic name to a Tag enum value."""
    return _TOPIC_TAG_MAP.get(topic, "array_basics")


def elo_for(difficulty: str) -> int:
    """Map difficulty string to ELO rating."""
    return {"easy": 1200, "medium": 1500, "hard": 1800}.get(difficulty, 1200)


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


def _generate_optimal_for_leetcode_sync(
    problem_id: int,
    title: str,
    description: str,
    difficulty: str,
    starter_code: str,
    sid: str,
) -> None:
    """Synchronous LLM call to generate optimal_solution for a LeetCode problem.

    Called from _generate_from_leetcode() during graph invoke.
    Uses get_stream_writer() for progress updates.
    """
    from code_tutor_agent.config import get_llm

    writer = get_stream_writer() or (lambda *a, **k: None)

    logger.info("Generating optimal_solution for LeetCode '%s' (%d)", title, problem_id)
    writer("🤖 正在生成最优解代码...")

    prompt = (
        f"你是一个算法专家。给定以下 LeetCode 题目，请写出最优解 Python 代码（class Solution 风格）：\n\n"
        f"标题: {title}\n"
        f"描述: {description}\n"
        f"难度: {difficulty}\n"
    )
    if starter_code:
        prompt += f"模板代码:\n{starter_code}\n"
    prompt += (
        "\n要求：\n"
        "- 使用最优算法（如哈希表、双指针、动态规划等）\n"
        "- 必须是可运行的合法 Python 代码\n"
        "- 方法签名必须准确\n"
        "- 只输出代码，不要任何解释\n"
    )

    try:
        llm = get_llm(purpose="generator")
        resp = llm.invoke(
            [("human", prompt)],
            config={"metadata": {"node": "generator", "step": "generate_problem"}},
        )
        code = resp.content if hasattr(resp, "content") else str(resp)
        # Strip markdown fences
        m = re.search(r"```python\n?(.*?)```", code, re.DOTALL)
        if m:
            code = m.group(1).strip()
        else:
            m = re.search(r"```\n?(.*?)```", code, re.DOTALL)
            if m:
                code = m.group(1).strip()
        code = code.strip()

        update_problem_optimal_solution(problem_id, code)
        writer(f"🤖 最优解代码已生成（{len(code)} 字符）")
        logger.info("Generated optimal_solution for LeetCode problem %d (%d chars)", problem_id, len(code))
    except Exception as exc:
        logger.warning("Failed to generate optimal_solution for LeetCode problem %d: %s", problem_id, exc)
        writer("⚠️ 最优解代码生成失败（不影响做题）")


def _generate_from_leetcode(
    sid: str,
    lc_data: dict[str, Any],
    existing_tutor_messages: list | None = None,
) -> Command[Literal["wait_for_submit_node"]]:
    """Build a problem directly from parsed LeetCode data.

    This path skips the LLM problem generation (title, description, tags,
    starter code all come from the LeetCode API), but still uses the LLM
    to generate the optimal solution code.
    """
    # stream writer: 后台 graph.invoke 无 stream 上下文时 get_stream_writer()
    # 返回 None，用 no-op 兜底避免崩溃；且本函数是独立函数，必须自己定义 writer，
    # 不能依赖 generator_node 的局部变量（否则 NameError）。
    writer = get_stream_writer() or (lambda *a, **k: None)

    title = lc_data.get("title", "LeetCode Problem")
    description = lc_data.get("description", "")
    difficulty = lc_data.get("difficulty", "medium")
    examples = lc_data.get("examples", [])
    starter_code = lc_data.get("starter_code", "")
    tags = lc_data.get("tags", [])
    hints = lc_data.get("hints", [])

    # Derive a topic from the first tag, or fall back to "算法"
    topic = tags[0] if tags else "算法"

    # ── 注入树/图/链表结构体定义到 starter_code ──
    struct_prologue = get_struct_prologue(topic, description, starter_code)
    if struct_prologue and not starter_code.startswith(struct_prologue.strip()[:20]):
        starter_code = struct_prologue + starter_code

    # Extract function_signature from starter_code
    func_sig = extract_function_signature(starter_code)

    # Build visible test cases from examples.
    # 优先用 parse_leetcode 已正确解析好的 parsed_test_cases（带函数签名推断），
    # 不再用 "" 重解析——否则裸 exampleTestcases（无 Input/Output 文本）的题
    # 会被按 1 个参数错误分组，生成错误用例。
    visible_tcs = lc_data.get("parsed_test_cases") or _parse_examples_to_test_cases(examples, starter_code)
    logger.info("Parsed %d visible test cases from LeetCode examples", len(visible_tcs))

    problem_dict = {
        "title": title,
        "topic": topic,
        "difficulty": difficulty,
        "description": description,
        "starter_code": starter_code,
        "test_cases": visible_tcs,
        "novelty_score": 9.0,
        "brute_solution": "",
        "function_signature": func_sig,
        # 落库约束条件：save_problem 已支持写入 constraints_json，
        # 后台 _generate_complex_tests 读 full.constraints 生成边界用例时
        # 就有约束引导（否则边界用例缺乏约束，覆盖面弱）。
        "constraints": lc_data.get("constraints", []),
    }

    # Save to DB (returns problem_id)
    problem_id = save_problem(problem_dict)

    # 调用 LLM 为该 LeetCode 题目生成最优解代码（同步，在当前 graph 线程中）
    _generate_optimal_for_leetcode_sync(problem_id, title, description, difficulty, starter_code, sid)

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
        tag_primary=tag_for(topic),
        prob_elo=elo_for(difficulty),
    )

    welcome_msg = TutorMsg(
        role="tutor",
        content=f"来自 LeetCode 的 **{title}** 🎯  \n\n"
                f"难度: {difficulty} | 标签: {', '.join(tags)}\n\n"
                f"编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。",
    )

    writer("✅ 题目已就绪！")

    return Command(
        update={
            "problem": meta,
            "status": "awaiting_submit",
        "phase": SessionPhase.solving,
            "submissions": [],
            "hint_level": 0,
            "tutor_messages": (existing_tutor_messages or []) + [welcome_msg],
            "last_verdict": None,
            "adversarial_triggered": False,
            "error_message": "",
            "leetcode": None,  # Clear so it's not reprocessed
            "_brute_code": "",
            "_function_signature": func_sig,
            "_problem_id": problem_id,
        },
        goto="wait_for_submit_node",
    )


def _take_visible(test_cases: list[dict], n: int = 2) -> list[dict]:
    """取前 n 条可见（非 hidden）测试用例。"""
    return [tc for tc in test_cases[:n] if not tc.get("is_hidden", False)]


def _self_verify_reference(brute_code: str, sample_tcs: list[dict], func_sig: str) -> bool:
    """用参考解在沙箱跑示例，验证其可执行并回填 expected_output。

    所有示例自验证通过返回 True；任一示例参考解崩溃/无输出则返回 False。
    sample_tcs 中的 expected_output 会被参考解的实际输出就地覆盖。
    """
    for tc in sample_tcs:
        input_args = tc.get("input_args", [])
        results = run_solution(brute_code, [tc], timeout=10.0, function_signature=func_sig, force_local=True)
        if not results:
            logger.warning("参考解自验证失败: TC %s 无结果", input_args)
            return False
        r = results[0]
        if r.status in ("Runtime Error", "TLE", "Judge Error"):
            logger.warning("参考解自验证失败: %s on %s", r.status, input_args)
            return False
        actual = r.detail or ""
        if not actual:
            logger.warning("参考解自验证失败: TC %s 无实际输出 (%s)", input_args, r.status)
            return False
        tc["expected_output"] = actual
    return True


def _generate_llm_problem(
    outcome: Any,
    sid: str,
    writer,
) -> tuple[dict | None, list[dict]]:
    """LLM 通道成功：解析示例 → 参考解自验证，产出可靠可见用例。

    返回 (problem_dict, sample_tcs)；任一步失败（无参考解 / 示例解析失败 /
    自验证不通过）返回 (None, []) 表示需回退静态题库。
    """
    problem_dict = outcome.problem.model_dump()
    brute_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "")
    if not brute_code:
        logger.warning("LLM 产出无 optimal_solution — 回退静态题库")
        return None, []
    examples = problem_dict.get("examples", [])
    func_sig = problem_dict.get("function_signature", "")
    logger.info("Parsing %d examples, sig=%s", len(examples), func_sig)
    _progress(sid, "🧪 正在解析示例测试用例…")
    writer("🧪 正在解析示例测试用例…")
    sample_tcs = _parse_examples_to_test_cases(examples, func_sig)
    if not sample_tcs:
        logger.warning("示例解析失败 — 回退静态题库")
        return None, []
    if _self_verify_reference(brute_code, sample_tcs, func_sig):
        logger.info("Lightweight generation OK — %d sample test cases", len(sample_tcs))
        _progress(sid, "✅ 题目已就绪！")
        writer("✅ 题目已就绪！")
        return problem_dict, sample_tcs
    logger.warning("示例自验证失败 — 回退静态题库")
    return None, []


def generator_node(state: SessionState) -> Command[Literal["wait_for_submit_node"]]:
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
    writer = get_stream_writer() or (lambda *a, **k: None)

    topic = state.topic
    difficulty = state.difficulty
    # 归一化：把口语知识点（如「图」）转成明确出题描述喂给 LLM/skill，
    # 但保留原始 topic 用于展示与 tag 归类（见下方 save_problem 覆盖回 topic）。
    gen_topic = normalize_topic_for_generation(topic)

    # ── 路径 A：LeetCode 导入（跳过 LLM 生成）──
    lc_data = state.leetcode
    if lc_data:
        logger.info("LeetCode data detected — skipping LLM generation")
        _progress(sid, "📥 使用 LeetCode 题目…")
        writer("📥 使用 LeetCode 题目…")
        return _generate_from_leetcode(
            sid, lc_data,
            existing_tutor_messages=state.tutor_messages if state.mode == "agent" else None,
        )

    # ── 出题：统一走 ProblemAgent（原生 LLM + 自校验 + 静态兜底，不依赖 skill-engine）──
    problem_dict: dict[str, Any] | None = None
    sample_tcs: list[dict] = []

    _progress(sid, "正在调用大模型生成题目…")
    writer("正在调用大模型生成题目…")

    outcome = ProblemAgent(topic=gen_topic, difficulty=difficulty).generate()

    if outcome.ok and outcome.channel == ProblemChannel.LLM:
        # LLM 出题成功：解析示例 → 参考解自验证，产出可靠可见用例
        problem_dict, sample_tcs = _generate_llm_problem(outcome, sid, writer)
    elif outcome.ok and outcome.channel == ProblemChannel.STATIC:
        # LLM 全失败，ProblemAgent 已回退到静态题库：直接采用，不二次拉取
        problem_dict = outcome.problem.model_dump()
        sample_tcs = _take_visible(problem_dict.get("test_cases", []))
        logger.info("出题走静态兜底（ProblemAgent 返回）: %s", problem_dict.get("title"))
        _progress(sid, "✅ 题目已就绪（静态兜底）！")
        writer("✅ 题目已就绪（静态兜底）！")

    # ── 极端兜底：LLM 与静态均不可用；强制再拉一次静态题库──
    if problem_dict is None:
        logger.warning("出题回退静态题库")
        _progress(sid, "⚠️ 大模型出题失败，切换到静态题库…")
        writer("⚠️ 大模型出题失败，切换到静态题库…")
        problem_dict = get_static_problem(topic=topic, difficulty=difficulty)
        if problem_dict is None:
            problem_dict = get_static_problem()
        logger.info("Fallback → %s", problem_dict.get("title", "unknown"))
        sample_tcs = _take_visible(problem_dict.get("test_cases", []))

    # ── 持久化到 DB — 先保存示例用例，完整用例后续补充 ──
    # The full test_suite will be generated in the background (API layer)
    # and saved via update_problem_test_cases()
    if problem_dict:
        problem_dict["test_cases"] = sample_tcs
        # skill / 静态池路径常常不产出 function_signature（或解析被漏掉），
        # 缺签名会让后台 _generate_complex_tests 退化成 0 用例、判题退化。
        # 优先用 parser 已解析的字段；缺失时从 starter_code 启发式提取。
        fsig = (problem_dict.get("function_signature") or "").strip()
        if not fsig:
            _starter = problem_dict.get("starter_code", "") or ""
            if _starter:
                fsig = extract_function_signature(_starter)
            problem_dict["function_signature"] = fsig
        # 展示用的知识点用用户原始输入，避免 skill 回填的长描述污染 UI
        problem_dict["topic"] = topic
    problem_id = save_problem(problem_dict or {})

    # If dedup happened (title already existed), reload from DB for correct starter_code
    db_problem = get_problem_by_id(problem_id)
    db_starter_code = db_problem.starter_code if db_problem else ""

    # Build visible test cases (sample ones)
    visible_tcs = [
        {
            "input_args": tc.get("input_args", []),
            "expected_output": tc.get("expected_output", ""),
            "explanation": tc.get("explanation", ""),
        }
        for tc in sample_tcs
    ]

    # ── 注入树/图/链表结构体定义到 starter_code ──
    _final_starter = db_starter_code or (problem_dict.get("starter_code", "") if problem_dict else "")
    _final_desc = problem_dict.get("description", "") if problem_dict else ""
    struct_prologue = get_struct_prologue(topic, _final_desc, _final_starter)
    if struct_prologue and not _final_starter.startswith(struct_prologue.strip()[:20]):
        _final_starter = struct_prologue + _final_starter

    meta = ProblemMeta(
        problem_id=problem_id,
        title=problem_dict.get("title", "Unknown") if problem_dict else "Unknown",
        topic=problem_dict.get("topic", topic) if problem_dict else topic,
        difficulty=problem_dict.get("difficulty", difficulty) if problem_dict else difficulty,
        description=problem_dict.get("description", "") if problem_dict else "",
        description_html=problem_dict.get("description", "") if problem_dict else "",
        starter_code=_final_starter,
        visible_test_cases=visible_tcs,
        novelty_score=problem_dict.get("novelty_score", 7.0) if problem_dict else 7.0,
        tag_primary=tag_for(topic),
        prob_elo=elo_for(difficulty),
    )

    # Store brute_code + function_signature in state for background test generation
    brute_code = problem_dict.get("optimal_solution", "") or problem_dict.get("brute_solution", "") if problem_dict else ""
    func_sig = problem_dict.get("function_signature", "") if problem_dict else ""

    welcome_msg = TutorMsg(
        role="tutor",
        content=f"来，试试这道 **{meta.title}**！\n\n编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。",
    )

    _progress(sid, "✅ 题目已就绪！")
    writer("✅ 题目已就绪！")

    # ── 返回 state — graph 路由到 wait_for_submit_node ──
    # The background test generation is triggered by the API layer
    # after _graph.invoke() returns (see _run_generation in api/main.py)
    # Agent 模式：保留出题前对话（连续对话），普通模式仅 welcome
    _tutor_msgs = (list(state.tutor_messages) if state.mode == "agent" else []) + [welcome_msg]
    update: dict[str, Any] = {
        "problem": meta,
        "status": "awaiting_submit",
        "phase": SessionPhase.solving,
        "submissions": [],
        "hint_level": 0,
        "tutor_messages": _tutor_msgs,
        "last_verdict": None,
        "adversarial_triggered": False,
        "error_message": "",
        # Store for background test generation (API layer reads these)
        "_brute_code": brute_code,
        "_function_signature": func_sig,
        "_problem_id": problem_id,
    }

    return Command(update=update, goto="wait_for_submit_node")