"""generation/ 包 — 出题子 Agent（docs/generation-subagent-design.md）。

收敛「散落的 3 层 fallback」为一棵确定性决策树，与外部主图（LangGraph）零耦合：
- ✗ 不 import：SessionState / Command / SessionPhase / graph/*
- ✗ 不 import LangGraph 类型
- ✓ 产出纯数据：ProblemDraft / GenerationResult（dataclass）
- ✓ 外部依赖全部走 Gateways 接口（可 mock，可在无 LangGraph 环境单测）
"""

from __future__ import annotations

from code_tutor_agent.generation.problem_generation_agent import MAX_RETRIES, ProblemGenerationAgent

__all__ = ["ProblemGenerationAgent", "MAX_RETRIES"]
