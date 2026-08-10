"""会话状态序列化：LangGraph state → JSON 安全字典（DTO 层）。"""
from __future__ import annotations

from typing import Any

from code_tutor_agent.progress import _generation_progress, get_generation_channel


def serialize_state(state: Any) -> dict:
    """Convert a LangGraph session state to a JSON-safe dict."""
    if isinstance(state, dict):
        d = state
    else:
        d = state.model_dump() if hasattr(state, "model_dump") else {}

    sid = d.get("session_id", "")
    progress = _generation_progress.get(sid, [])

    raw_problem = d.get("problem")
    problem = None
    if raw_problem:
        pd = raw_problem.model_dump() if hasattr(raw_problem, "model_dump") else raw_problem
        pd.setdefault("starter_code", "")
        pd.setdefault("visible_test_cases", [])
        problem = pd

    return {
        "session_id": sid,
        "topic": d.get("topic", ""),
        "difficulty": d.get("difficulty", ""),
        "mode": d.get("mode", ""),
        "status": d.get("status", "generating"),
        "phase": d.get("phase", "solving"),
        "total_problems": d.get("total_problems", 0),
        "problem_history": [
            r.model_dump() if hasattr(r, "model_dump") else r
            for r in (d.get("problem_history") or [])
        ],
        "problem": problem,
        "submissions": [
            {
                "index": s.get("index") if isinstance(s, dict) else s.index,
                "code": (s.get("code", "") if isinstance(s, dict) else s.code),
                "language": s.get("language", "python") if isinstance(s, dict) else s.language,
                "verdict": s.get("verdict", "") if isinstance(s, dict) else getattr(s, "verdict", ""),
                "timestamp": s.get("timestamp", "") if isinstance(s, dict) else getattr(s, "timestamp", ""),
                "judge_results": [r.model_dump() if hasattr(r, "model_dump") else r for r in (s.get("judge_results", []) if isinstance(s, dict) else s.judge_results)],
                "hint_level_given": s.get("hint_level_given", 0) if isinstance(s, dict) else s.hint_level_given,
            }
            for s in (d.get("submissions") or [])
        ],
        "tutor_messages": [
            m.model_dump() if hasattr(m, "model_dump") else m
            for m in (d.get("tutor_messages") or [])
        ],
        "hint_level": d.get("hint_level", 0),
        "last_verdict": d.get("last_verdict"),
        "last_review_payload": d.get("last_review_payload"),
        "error_message": d.get("error_message", ""),
        # 出题命中通道透出（llm / leetcode_import / leetcode_pull / db_unac / static）
        "channel": get_generation_channel(sid),
        "progress_messages": progress,
        "repair_suggestion": d.get("repair_suggestion", ""),
        "warm_feedback": d.get("warm_feedback", ""),
        "judge_cycle": d.get("judge_cycle", 0),
    }


def empty_state(sid: str) -> dict:
    """Return a minimal state dict when the session is still being generated."""
    progress = _generation_progress.get(sid, [])
    return {
        "session_id": sid,
        "topic": "",
        "difficulty": "",
        "mode": "practice",
        "status": "generating",
        "problem": None,
        "submissions": [],
        "tutor_messages": [],
        "hint_level": 0,
        "last_verdict": None,
        "last_review_payload": None,
        "error_message": "",
        "progress_messages": progress,
    }
