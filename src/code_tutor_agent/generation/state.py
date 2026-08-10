"""出题子 Agent 的纯数据对象（无 Pydantic / LangGraph 依赖）。

见 docs/generation-subagent-design.md §6。全部为原生 dataclass，
保证 generation/ 包可在无 LangGraph 环境下独立单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class GenerationOptions:
    """出题选项。"""

    max_retries: int = 3
    dual_solution: bool = False   # 导入题是否补暴力解（+1 次 LLM/题）


@dataclass
class GenerationContext:
    """一次出题的完整上下文（带默认值，字段可扩展）。"""

    topic: str
    difficulty: str
    lc_url: str | None = None            # 用户贴的 LeetCode URL（导入通道）
    leetcode: dict | None = None         # 已解析的 LeetCode dict（/leetcode/parse 产物）
    profile_hint: str | None = None      # 用户画像弱项提示（HISTORY 优先级用）
    options: GenerationOptions = field(default_factory=GenerationOptions)


@dataclass
class ProblemDraft:
    """一道待落库题目的完整数据（设计 §6）。"""

    topic: str
    difficulty: str
    title: str
    description: str
    starter_code: str
    optimal_solution: str = ""
    brute_solution: str = ""
    examples: list[str] = field(default_factory=list)      # 原始示例文本（LLM/LeetCode 输出）
    constraints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    function_signature: str = ""
    test_cases: list[dict] = field(default_factory=list)   # 落库时的可见用例
    source_slug: str = ""                                  # 非空 = 题目来自 LeetCode（import/pull）
    imported: bool = False                                 # 导入命中标记（dict 导入无 slug）

    @property
    def from_leetcode(self) -> bool:
        """题目来自 LeetCode（导入或拉题），落库 source/novelty 据此判定。

        dict 导入（/leetcode/parse 产物）不含 url/slug，仅靠 ``source_slug``
        会误判为「原创」（2026-08-10 修复，docs/generation-subagent-design.md §7）。
        """
        return bool(self.source_slug) or self.imported


@dataclass
class GenerationResult:
    """一次出题的结果：题目 + 实际命中通道 + 失败原因（设计 §6）。"""

    ok: bool
    channel: str | None                  # llm / leetcode_import / leetcode_pull / db_unac / static
    problem_id: int | None = None
    draft: ProblemDraft | None = None
    test_cases_ready: bool = False       # 后台补全契约
    fallback_chain: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class GenEvent:
    """进度 / 警告 / 错误事件（设计 §6）。"""

    kind: str                            # progress / warning / error
    message: str


class ProgressSink(Protocol):
    """进度事件接收器：由调用方（如 generator_node）注入，包内不感知传输方式。"""

    def event(self, ev: GenEvent) -> None: ...


class NullSink:
    """无操作 sink（默认值，测试与无 UI 环境直接用）。"""

    def event(self, ev: GenEvent) -> None:
        pass
