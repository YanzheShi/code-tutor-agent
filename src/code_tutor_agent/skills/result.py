"""双通道共用结果结构（DP-5 / Phase 4）。

``SkillResult`` 是 import 主通道（``engine_adapter.run_skill``）与 CLI 逃生舱
（``skill_cli.run_skill_cli``）**共用**的返回结构，使上层（``tools.py`` 的
``mode`` 路由）不感知通道差异，统一以 ``.ok`` / ``.output`` / ``.error`` 消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillResult:
    skill_name: str
    ok: bool
    output: str = ""                 # 主文本输出（markdown）
    artifacts: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    error: str | None = None
