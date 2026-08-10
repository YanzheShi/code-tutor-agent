"""gateways/ 包 — 外部依赖薄封装（设计 §8），全部可 mock。"""

from __future__ import annotations

from code_tutor_agent.generation.gateways.leetcode import LeetCodeGateway
from code_tutor_agent.generation.gateways.llm import LlmGateway
from code_tutor_agent.generation.gateways.sandbox import SandboxGateway
from code_tutor_agent.generation.gateways.store import StoreGateway

__all__ = ["LeetCodeGateway", "LlmGateway", "StoreGateway", "SandboxGateway"]
