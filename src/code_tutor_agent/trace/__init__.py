"""轨迹分析模块（按题隔离、多轮追问、独立线程、绝不回灌画像）。

对外入口：
- first_round_analysis(session_id, problem_id, problem_meta=None) -> AnalysisResult
- continue_analysis(session_id, problem_id, message) -> str
- summarize_thread(session_id, problem_id, transition_action) -> TraceSummary
- archive_thread(session_id, problem_id) -> None
- list_thread_for_display(session_id, problem_id) -> [{"role","content"}, ...]
"""
from code_tutor_agent.trace.agent import (
    archive_thread,
    continue_analysis,
    first_round_analysis,
    list_thread_for_display,
)
from code_tutor_agent.trace.summarize import summarize_thread

__all__ = [
    "first_round_analysis",
    "continue_analysis",
    "summarize_thread",
    "archive_thread",
    "list_thread_for_display",
]
