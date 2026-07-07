"""Problem generator agent: produces coding problems via LLM structured output.

D2: supports the expanded Problem model with dual solutions + adversarial spec.
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
    """Day2: verify that brute_solution compiles and runs.

    No test cases exist yet at generation time — those are generated
    locally later.  We just check that the brute_solution is valid
    Python and can be imported.
    """
    logger.info("▶ verify_problem() — checking brute_solution compiles")
    brute = _extract_code(problem_dict.get("brute_solution", ""))
    if not brute:
        logger.warning("No brute_solution — cannot verify")
        return False
    try:
        compile(brute, "<brute_solution>", "exec")
        logger.info("✓ brute_solution compiles OK")
        return True
    except SyntaxError as exc:
        logger.warning("brute_solution syntax error: %s", exc)
        return False


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
    llm = get_llm(model_alias, temperature=0.7)
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

    # Return last attempt even if verification failed
    logger.warning("Max retries reached — returning last generated problem")
    return problem