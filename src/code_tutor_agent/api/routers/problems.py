"""Problems router — list / get problems and submissions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/problems")
async def list_problems():
    """List all problems in the database."""
    from code_tutor_agent.db.database import get_all_problem_ids, get_problem_by_id

    ids = get_all_problem_ids()
    result = []
    for pid in ids[-50:]:
        p = get_problem_by_id(pid)
        if p:
            result.append({
                "id": p["id"],
                "title": p.get("title", ""),
                "topic": p.get("topic", ""),
                "difficulty": p.get("difficulty", ""),
            })
    return {"problems": result}


@router.get("/problem/{problem_id}/submissions")
async def get_problem_submissions(problem_id: int):
    """Get persistent submission history for a problem."""
    from code_tutor_agent.db.database import get_submissions_by_problem
    return {"submissions": get_submissions_by_problem(problem_id)}