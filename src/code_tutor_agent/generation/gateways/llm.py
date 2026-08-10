"""LlmGateway — 出题 / 补解 / 边界用例 LLM 薄封装（设计 §8）。

底层 ``get_llm(purpose=...)``；单次生成尝试（重试由编排层决策树负责）。
"""

from __future__ import annotations

import json
import logging
import re

from code_tutor_agent.generation.state import ProblemDraft

logger = logging.getLogger(__name__)


# ── 中文知识点口语 → 规范化出题描述 ──
# 典型坑：用户说「图」被 skill/LLM 理解成「图片 / 网格矩阵」，实际指图论
# （graph theory）。这里把口语归一化为明确的出题描述后再喂给 LLM。
_TOPIC_GEN_MAP: dict[str, str] = {
    "图": (
        "图论（graph theory：顶点与边的数据结构与算法，例如图的 DFS/BFS 遍历、"
        "连通分量、最短路径、拓扑排序；注意：不是图片/图像/像素，也不是二维网格矩阵 grid）"
    ),
    "图论": (
        "图论（graph theory：顶点与边的数据结构与算法，例如图的 DFS/BFS 遍历、"
        "连通分量、最短路径、拓扑排序）"
    ),
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

    def generate_problem(self, topic: str, difficulty: str) -> ProblemDraft | None:
        """单次结构化出题尝试；失败（异常或自校验不过）返回 None，由上层重试。

        注意 max_retries=1（一次真实 LLM 调用）：外层 ``ProblemGenerationAgent.run``
        的 for 循环负责重试（默认 MAX_RETRIES 次），这里传 0 会让
        ``agent_problem.generate_problem`` 的 ``range(0)`` 一次都不执行、直接抛错，
        导致整个 LLM 原创通道永远失败（2026-08-10 修复）。
        """
        from code_tutor_agent.agents.agent_problem import generate_problem as _llm_generate_problem

        gen_topic = normalize_topic_for_generation(topic)
        try:
            p = _llm_generate_problem(gen_topic, difficulty, purpose="problem", max_retries=1)
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
    ) -> str | None:
        """为 LeetCode 导入题生成最优解代码（镜像 _generate_optimal_for_leetcode_sync）。"""
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
        try:
            llm = get_llm(purpose="generator")
            resp = llm.invoke([("human", prompt)])
            text = resp.content if hasattr(resp, "content") else str(resp)
            code = _extract_code(text)
            return code or None
        except Exception as exc:
            logger.warning("最优解生成失败: %s", exc)
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
        try:
            llm = get_llm(purpose="api-generation-high")
            resp = llm.invoke([("system", GENERATE_BOUNDARY_SYSTEM), ("human", prompt_user)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if not json_match:
                logger.warning("边界用例响应无 JSON")
                return None
            cases = json.loads(json_match.group(0))
            return cases if isinstance(cases, list) else None
        except Exception as exc:
            logger.warning("边界用例生成失败: %s", exc)
            return None
