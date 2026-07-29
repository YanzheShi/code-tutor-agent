"""向后兼容 shim：出题逻辑已迁入 agents/agent_problem.py（Problem Agent）。

保留本模块仅为不破坏既有 import（nodes/generator.py、benchmark 脚本、存量测试）。
新代码请直接：

    from code_tutor_agent.agents.agent_problem import generate_problem, ProblemAgent, verify_problem
"""

from code_tutor_agent.agents.agent_problem import (  # noqa: F401
    GenerationOutcome,
    ProblemAgent,
    ProblemChannel,
    _extract_code,
    _flat_to_problem,
    generate_detailed_solution,
    generate_problem,
    verify_problem,
)
# 保留旧 import 目标，避免 ``from code_tutor_agent.agents.problem_generator import get_llm`` 断链。
from code_tutor_agent.config import get_llm  # noqa: F401
