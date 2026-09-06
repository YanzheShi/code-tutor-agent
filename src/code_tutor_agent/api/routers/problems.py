"""Problems router — list / get problems and submissions (multi-user aware)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from code_tutor_agent.api.auth import get_current_user, user_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/problems")
async def list_problems(current: dict = Depends(get_current_user)):
    """List all problems in the database — batch query for performance.

    Includes latest verdict per problem for the "已AC/已提交" icon display.
    题库全用户共享，但 verdict 按当前用户过滤（进度各自独立）。
    """
    from code_tutor_agent.db.database import get_all_problem_ids, get_problems_by_ids, get_all_problem_verdicts

    ids = get_all_problem_ids()
    problems = get_problems_by_ids(ids)
    verdicts = get_all_problem_verdicts(user_key(current))
    return {"problems": [
        {
            "id": p["id"],
            "title": p.get("title", ""),
            "topic": p.get("topic", ""),
            "difficulty": p.get("difficulty", ""),
            "verdict": verdicts.get(p["id"], ""),
        }
        for p in problems
    ]}


@router.get("/topics")
async def list_topics():
    """主题目录 — 前端出题选择器渲染按钮用（值即中文主题名，与后端生成链路一致）。"""
    from code_tutor_agent.topics import TOPICS_RESPONSE

    return TOPICS_RESPONSE


@router.get("/problem/{problem_id}/submissions")
async def get_problem_submissions(
    problem_id: int, current: dict = Depends(get_current_user),
):
    """Get persistent submission history for a problem (当前用户的提交记录)。"""
    from code_tutor_agent.db.database import get_submissions_by_problem
    return {"submissions": get_submissions_by_problem(problem_id, user_id=user_key(current))}