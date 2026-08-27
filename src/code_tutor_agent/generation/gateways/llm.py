"""LlmGateway — 出题 / 补解 / 边界用例 LLM 薄封装（设计 §8）。

底层 ``get_llm(purpose=...)``；单次生成尝试（重试由编排层决策树负责）。
"""

from __future__ import annotations

import json
import logging
import re
import time

from code_tutor_agent.generation.state import ProblemDraft

logger = logging.getLogger(__name__)

# 边界用例单条输入字符数硬上限：超过即视为 LLM 又吐了巨数组，直接丢弃该条
# （而非整批解析失败）。300 对语义边界用例绰绰有余，能挡住 token 爆炸。
_BOUNDARY_MAX_INPUT_CHARS = 300


# ── 中文知识点口语 → 规范化出题描述 ──
# 典型坑：用户说「图」被 skill/LLM 理解成「图片 / 网格矩阵」，实际指图论
# （graph theory）。这里把口语归一化为明确的出题描述后再喂给 LLM。
_TOPIC_GEN_MAP: dict[str, str] = {
    "图": (
        "图论（graph theory：顶点与边的数据结构与算法；"
        "注意：不是图片/图像/像素，也不是二维网格矩阵 grid）"
    ),
    "图论": (
        "图论（graph theory：顶点与边的数据结构与算法）"
    ),
    "图遍历": "图论遍历（图的 DFS/BFS，邻接表/邻接矩阵表示，含 visited 集合防环）",
    "图的bfs": "图的广度优先搜索（BFS，队列实现，邻接表/邻接矩阵表示）",
    "图的dfs": "图的深度优先搜索（DFS，递归或栈，邻接表/邻接矩阵表示）",
    "拓扑排序": "拓扑排序（topological sort，有向无环图 DAG，Kahn 算法或 DFS 后序）",
    "最短路径": "最短路径（图论，Dijkstra / Bellman-Ford / Floyd）",
    "并查集": "并查集（union-find / disjoint set，路径压缩 + 按秩合并）",
    "树": "树 / 二叉树（tree：节点含子指针的层级结构，含根/叶/深度/子树等概念）",
    "二叉树": "二叉树（binary tree：每个节点至多两个子节点的树形结构）",
    "堆": "堆 / 优先队列（heap / priority queue：完全二叉树结构，支持取最值）",
    "优先队列": "优先队列（priority queue，heap 实现）",
    "回溯": "回溯（backtracking：通过尝试与撤销选择搜索解空间的算法范式）",
    "贪心": "贪心（greedy：每步取局部最优以推导全局最优的算法范式）",
    "位运算": "位运算（bit manipulation：按位与或异或、移位、掩码、lowbit 等）",
    "数论": "数论（number theory：质数、GCD、模运算等整数性质）",
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
    # 子串匹配仅接受「词首命中 + 键长 ≥ 2」：单字键（图/树）会被
    # 「图像处理」「树状数组」误命中，导致出题偏题（2026-08-10 修复）。
    for key in sorted(_TOPIC_GEN_MAP, key=len, reverse=True):
        if len(key) >= 2 and t.startswith(key):
            return _TOPIC_GEN_MAP[key]
    return t


def _problem_to_draft(p, topic: str, difficulty: str) -> ProblemDraft | None:
    """把 ProblemAgent 产物（Problem）转成 ProblemDraft。"""
    if p is None:
        return None
    try:
        return ProblemDraft(
            topic=topic,
            difficulty=difficulty,
            title=p.title,
            description=p.description,
            starter_code=p.starter_code or "",
            optimal_solution=p.optimal_solution or "",
            brute_solution=p.brute_solution or "",
            examples=list(p.examples or []),
            constraints=list(p.constraints or []),
            function_signature=p.function_signature or "",
            test_cases=list(p.test_cases or []),
        )
    except Exception as exc:
        logger.warning("Problem → ProblemDraft 转换失败: %s", exc)
        return None


def _extract_code(text: str) -> str:
    """提取 markdown 围栏内的 Python 代码。"""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    if match := re.search(r"```\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()


class LlmGateway:
    """LLM 薄封装：出题 / 补解（optimal、brute）/ 边界用例。"""

    def generate_problem(
        self, topic: str, difficulty: str,
        user_suffix: str | None = None,
    ) -> ProblemDraft | None:
        """单次结构化出题尝试；失败（异常或自校验不过）返回 None，由上层重试。

        注意 max_retries=1（一次真实 LLM 调用）：外层 ``ProblemGenerationAgent.run``
        的 for 循环负责重试（默认 MAX_RETRIES 次），这里传 0 会让
        ``agent_problem.generate_problem`` 的 ``range(0)`` 一次都不执行、直接抛错，
        导致整个 LLM 原创通道永远失败（2026-08-10 修复）。

        user_suffix：方案 H（F/G 随机二选一）注入段，追加到 USER prompt 末尾。
        """
        from code_tutor_agent.agents.agent_problem import generate_problem as _llm_generate_problem

        gen_topic = normalize_topic_for_generation(topic)
        try:
            p = _llm_generate_problem(
                gen_topic, difficulty, purpose="problem", max_retries=1,
                user_suffix=user_suffix,
            )
            return _problem_to_draft(p, topic, difficulty)
        except Exception as exc:
            logger.warning("LLM 出题单次尝试失败: %s", exc)
            return None

    def generate_optimal(
        self,
        title: str,
        description: str,
        difficulty: str,
        starter_code: str = "",
        function_signature: str = "",
        max_retries: int = 3,
    ) -> str | None:
        """为 LeetCode 导入题生成最优解代码（镜像 _generate_optimal_for_leetcode_sync）。

        带重试：单次 LLM 调用可能返回空响应或抛异常（网络抖动、模型空响应），
        最多尝试 ``max_retries`` 次（含首次），非末次失败后简单退避再试，
        全部失败才返回 ``None``。调用方（``_import_from_leetcode``）会把 ``None``
        当作致命错误上报，因此这里尽量通过重试消化瞬时失败，避免一道没有参考解
        的题目被静默导入（缺参考解会导致后续判题无法运行学生提交）。
        """
        from code_tutor_agent.config import get_llm

        prompt = (
            f"你是一个算法专家。给定以下 LeetCode 题目，请写出最优解 Python 代码"
            f"（class Solution 风格）：\n\n"
            f"标题: {title}\n"
            f"描述: {description}\n"
            f"难度: {difficulty}\n"
        )
        if function_signature:
            prompt += f"函数签名: {function_signature}\n"
        if starter_code:
            prompt += f"模板代码:\n{starter_code}\n"
        prompt += (
            "\n要求：\n"
            "- 使用最优算法（如哈希表、双指针、动态规划等）\n"
            "- 必须是可运行的合法 Python 代码\n"
            "- 方法签名必须准确\n"
            "- 只输出代码，不要任何解释\n"
        )
        llm = get_llm(purpose="generator")
        for attempt in range(max_retries):
            try:
                resp = llm.invoke([("human", prompt)])
                text = resp.content if hasattr(resp, "content") else str(resp)
                code = _extract_code(text)
                if code:
                    return code
                logger.warning("最优解生成为空（attempt %d/%d），重试", attempt + 1, max_retries)
            except Exception as exc:
                logger.warning("最优解生成失败（attempt %d/%d）: %s", attempt + 1, max_retries, exc)
            # 非末次尝试：简单退避后再试（避免对瞬时故障猛打）
            if attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
        logger.warning("最优解生成在 %d 次尝试后仍失败", max_retries)
        return None

    def generate_dual(self, description: str, starter_code: str, mode: str) -> str | None:
        """补生成对拍解：mode="brute" 生成暴力解（补双解用）。"""
        from code_tutor_agent.config import get_llm

        mode_desc = {
            "brute": "暴力解（直观、可能低效但正确，用于对拍验证最优解）",
        }.get(mode, "题解")
        prompt = (
            f"你是一个算法专家。给定以下题目，请写出{mode_desc} Python 代码"
            f"（class Solution 风格）：\n\n"
            f"描述: {description}\n"
        )
        if starter_code:
            prompt += f"模板代码:\n{starter_code}\n"
        prompt += (
            "\n要求：\n"
            "- 必须是可运行的合法 Python 代码\n"
            "- 方法签名必须与模板一致\n"
            "- 只输出代码，不要任何解释\n"
        )
        try:
            llm = get_llm(purpose="generator")
            resp = llm.invoke([("human", prompt)])
            text = resp.content if hasattr(resp, "content") else str(resp)
            code = _extract_code(text)
            return code or None
        except Exception as exc:
            logger.warning("补解（%s）生成失败: %s", mode, exc)
            return None

    def generate_boundary(
        self,
        *,
        title: str,
        description: str,
        difficulty: str,
        function_signature: str,
        constraints: list[str],
        optimal_code: str,
        existing_cases: list[dict],
        count: int = 8,
    ) -> list[dict] | None:
        """LLM 生成边界测试用例（镜像 _generate_complex_tests 的 Prompt B）。"""
        from code_tutor_agent.config import get_llm
        from code_tutor_agent.prompts.generate_boundary_cases import (
            GENERATE_BOUNDARY_SYSTEM,
            GENERATE_BOUNDARY_USER,
        )

        existing_str = "\n".join(
            f"  #{i + 1}: input_args={tc.get('input_args', [])} → {tc.get('expected_output', '')}"
            for i, tc in enumerate(existing_cases[:4])
        )
        constraints_str = "\n".join(f"  - {c}" for c in (constraints or []))
        prompt_user = GENERATE_BOUNDARY_USER.format(
            title=title,
            description=description,
            difficulty=difficulty,
            function_signature=function_signature,
            constraints=constraints_str,
            optimal_code=optimal_code,
            existing_cases=existing_str,
            count=count,
        )
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                llm = get_llm(purpose="api-generation-high")
                resp = llm.invoke(
                    [("system", GENERATE_BOUNDARY_SYSTEM), ("human", prompt_user)]
                )
                content = resp.content if hasattr(resp, "content") else str(resp)
                if attempt == 0:
                    # 截断日志，避免巨型响应刷屏（曾因性能用例巨数组导致日志爆炸）
                    logger.info("生成边界测试用例: %s", content[:2000])
                json_match = re.search(r"\[.*\]", content, re.DOTALL)
                if not json_match:
                    raise ValueError("响应中未找到 JSON 数组")
                cases = json.loads(json_match.group(0))
                if not isinstance(cases, list):
                    raise ValueError("响应顶层不是 JSON 数组")
                # 过滤超长/畸形用例，避免单条巨数组拖垮整批
                kept: list[dict] = []
                for c in cases:
                    ias = c.get("input_args") if isinstance(c, dict) else None
                    if not isinstance(ias, list):
                        continue
                    if sum(len(str(a)) for a in ias) > _BOUNDARY_MAX_INPUT_CHARS:
                        logger.warning(
                            "边界用例输入过大被跳过: %s",
                            (c.get("explanation", "") if isinstance(c, dict) else ""),
                        )
                        continue
                    kept.append(c)
                return kept or None
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("边界用例解析失败(第%d次): %s", attempt + 1, exc)
                if attempt == 0:
                    # 首次失败：追加强化约束后重试一次，避免整批静默丢失
                    prompt_user = (
                        prompt_user
                        + "\n\n[重试] 必须只输出一个合法、完整、闭合的 JSON 数组，"
                        "且不含任何额外文字、不生成超大/达约束上限的数组。"
                        "每个 input_args 必须很小（总字符数 < 200）。"
                    )
        logger.warning("边界用例生成失败: %s", last_exc)
        return None
