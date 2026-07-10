"""Problems router — list / get problems and submissions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/problems")
async def list_problems():
    """List all problems in the database — batch query for performance."""
    from code_tutor_agent.db.database import get_all_problem_ids, get_problems_by_ids

    ids = get_all_problem_ids()
    problems = get_problems_by_ids(ids[-50:])
    return {"problems": [
        {
            "id": p["id"],
            "title": p.get("title", ""),
            "topic": p.get("topic", ""),
            "difficulty": p.get("difficulty", ""),
        }
        for p in problems
    ]}


@router.get("/problem/{problem_id}/submissions")
async def get_problem_submissions(problem_id: int):
    """Get persistent submission history for a problem."""
    from code_tutor_agent.db.database import get_submissions_by_problem
    return {"submissions": get_submissions_by_problem(problem_id)}