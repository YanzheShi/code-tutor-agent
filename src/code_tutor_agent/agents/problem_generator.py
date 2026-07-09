"""题目生成 Agent：通过 LLM 结构化输出生成编程题。

D2：支持扩展后的 Problem 模型（含 optimal_solution）。
"""

from __future__ import annotations

import logging
import re

from langchain_core.prompts import ChatPromptTemplate

from code_tutor_agent.config import get_llm
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.prompts.generate_problem import (
    GENERATE_PROBLEM_SYSTEM,
    GENERATE_PROBLEM_USER,
)

logger = logging.getLogger(__name__)


def _extract_code(solution_text: str) -> str:
    """Extract Python code from LLM response, stripping markdown fences."""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", solution_text, re.DOTALL):
        return match.group(1).strip()
    return solution_text.strip()


def verify_problem(problem_dict: dict) -> bool:
    """Day2: verify that optimal_solution compiles and runs.

    No test cases exist yet at generation time — those are generated
    locally later.  We just check that the optimal_solution is valid
    Python and that the description doesn't contain chain-of-thought.
    Also checks starter_code is valid; if not, derives it from optimal_solution.
    """
    logger.info("▶ verify_problem() — checking optimal_solution compiles")
    optimal = _extract_code(problem_dict.get("optimal_solution", ""))
    if not optimal:
        logger.warning("No optimal_solution — cannot verify")
        return False

    # 检查描述中是否有思考过程泄漏
    desc = problem_dict.get("description", "")
    cot_keywords = ["让我们", "再试一个", "试一个", "选择", "这道题", "其实", "但是", "再试", "经典的", "标准题"]
    if any(kw in desc for kw in cot_keywords):
        logger.warning("Description contains chain-of-thought — rejecting")
        return False

    try:
        compile(optimal, "<optimal_solution>", "exec")
        logger.info("✓ optimal_solution compiles OK")
    except SyntaxError as exc:
        logger.warning("optimal_solution syntax error: %s", exc)
        return False

    # 如果 LLM 没有提供合法的 starter_code，从 optimal_solution 自动生成
    sc = problem_dict.get("starter_code", "") or ""
    if not sc or "class Solution" not in sc or "def " not in sc:
        logger.info("starter_code is missing or invalid — deriving from optimal_solution")
        # 从 optimal_solution 提取方法签名
        import re
        # 匹配：class Solution:\n    def method_name(self, params) -> return_type:
        method_match = re.search(
            r'class Solution:\s+def (\w+)\(self([^)]*)\)\s*(?:->\s*(\w+(?:\[.*?\])?))?',
            optimal,
        )
        if method_match:
            method_name = method_match.group(1)
            params = method_match.group(2).strip()
            ret_type = method_match.group(3)
            ret_anno = f" -> {ret_type}" if ret_type else ""
            # 生成正确的 starter_code：class + 方法签名 + pass
            sc_generated = f"class Solution:\n    def {method_name}(self, {params}){ret_anno}:\n        pass\n"
            problem_dict["starter_code"] = sc_generated
            # 同时从方法生成 function_signature
            sig_parts = optimal.split("def " + method_name, 1)
            if len(sig_parts) > 1:
                sig_line = sig_parts[1].split("\n")[0].strip()
                # 去掉 'self, ' 前缀
                sig_line = re.sub(r'^\(self,\s*', '(', sig_line)
                sig_line = re.sub(r'^\(self\)', '()', sig_line)
                problem_dict["function_signature"] = sig_line
            logger.info("Generated starter_code from optimal_solution: %s(...)", method_name)
        else:
            logger.warning("Could not parse method from optimal_solution — keeping as-is")

    return True


def generate_problem(
    topic: str,
    difficulty: str,
    model_alias: str = "agnes",
    max_retries: int = 2,
) -> Problem:
    """Generate a coding problem with structured output.

    Uses ``llm.with_structured_output(Problem)`` for reliable JSON parsing.

    Args:
        topic: Knowledge point (e.g. "数组", "双指针").
        difficulty: "easy", "medium", or "hard".
        model_alias: LLM alias from config.
        max_retries: Extra regeneration attempts after first try.

    Returns:
        A fully populated Problem instance.
    """
    logger.info("▶ generate_problem() — topic=%s difficulty=%s", topic, difficulty)
    llm = get_llm(model_alias, temperature=0.2)
    structured_llm = llm.with_structured_output(Problem)

    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATE_PROBLEM_SYSTEM),
        ("human", GENERATE_PROBLEM_USER),
    ])

    chain = prompt | structured_llm

    for attempt in range(max_retries + 1):
        logger.info("LLM call attempt %d/%d …", attempt + 1, max_retries + 1)

        try:
            problem = chain.invoke({"topic": topic, "difficulty": difficulty})
        except Exception as exc:
            logger.warning("LLM structured output failed: %s", exc)
            continue

        problem_dict = problem.model_dump()

        if verify_problem(problem_dict):
            return problem

        logger.warning("Self-verification failed on attempt %d — retrying", attempt + 1)

    # 即使验证失败也返回最后一次尝试的结果
    logger.warning("Max retries reached — returning last generated problem")
    return problem