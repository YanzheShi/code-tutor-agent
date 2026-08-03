"""等待提交节点 — 暂停 graph 并收集用户代码。

这是调用 LangGraph ``interrupt()`` 的唯一位置，
使 graph 暂停并等待前端 POST /submit。

节点流转：
    generator_node → wait_for_submit_node（interrupt 暂停）
        ├── agent 模式 → agent_judge_node
        └── 普通模式 → judge_node
"""

from __future__ import annotations

import logging

from langgraph.types import interrupt

from code_tutor_agent.schemas.state import SessionState, Submission

logger = logging.getLogger(__name__)


def wait_for_submit_node(state: SessionState) -> dict:
    """Pause execution and wait for the user to submit code.

    The ``interrupt(value)`` call sends a structured payload to the
    FastAPI layer (which returns it as the HTTP response).  When the
    frontend calls ``POST /session/{sid}/submit {code}``, the LG
    runtime resumes here with the user's code as the return value.

    Returns:
        A partial state update with the new ``Submission`` appended.
    """
    logger.info("▶ wait_for_submit_node()")
    payload = {
        "type": "awaiting_submit",
        "problem": state.problem.model_dump() if state.problem else None,
        "submission_count": len(state.submissions),
        "hint_level": state.hint_level,
        "last_verdict": state.last_verdict,
    }

    # ── Pause here — resume value comes from Command(resume=...) ──
    resume_data = interrupt(payload)

    # ── 换题（abandon）路径：/next-problem 以 {"abandon": True, "preference": ...}
    #    恢复本中断。不产生提交，只携带放弃标记与选题偏好，由
    #    wait_for_submit_router 路由到 critic_node 完成换题。
    #    （历史上 /next-problem 用 update_state(as_node="critic_node")+invoke(None)
    #      在暂停期写状态，中断会丢失且 critic 不会真正运行 → 永远不出新题，
    #      2026-08-04 改为经 resume 通道传递。）──
    if isinstance(resume_data, dict) and resume_data.get("abandon"):
        logger.info("Resumed with abandon flag → pending_abandon=True, preference=%s",
                    resume_data.get("preference"))
        return {
            "pending_abandon": True,
            "next_preference": resume_data.get("preference"),
        }

    # ── Extract user code from resumed data ──
    code = ""
    language = "python"
    if isinstance(resume_data, dict):
        code = resume_data.get("code", "")
        language = resume_data.get("language", "python")
    elif isinstance(resume_data, str):
        code = resume_data

    if not code:
        logger.warning("Resumed with empty code — keeping state unchanged")
        return {}

    new_sub = Submission(
        index=len(state.submissions) + 1,
        code=code,
        language=language,
        timestamp=__import__("datetime").datetime.now().isoformat(),
    )

    logger.info(
        "Resumed → submission #%d (%d chars, lang=%s)",
        new_sub.index, len(code), language,
    )

    return {
        "submissions": [new_sub],
        "status": "judging",
    }