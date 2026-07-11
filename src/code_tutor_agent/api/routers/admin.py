"""Admin router — password-protected management endpoints."""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, HTTPException

from code_tutor_agent.schemas.api import (
    AdminLoginRequest,
    AdminPasswordRequest,
    AdminProblemOut,
    AdminUpdateProblemRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_ADMIN_PASSWORD: str | None = None


def _get_admin_password() -> str | None:
    global _ADMIN_PASSWORD
    if _ADMIN_PASSWORD is None:
        _ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
    return _ADMIN_PASSWORD


def _verify_admin(request_body: dict) -> bool:
    expected = _get_admin_password()
    if not expected:
        return True
    provided = (request_body or {}).get("password", "")
    return provided == expected


@router.post("/login")
async def admin_login(body: AdminLoginRequest):
    """Verify admin password."""
    expected = _get_admin_password()
    if not expected:
        return {"ok": True, "message": "Admin mode (no password configured)"}
    if body.password == expected:
        return {"ok": True, "message": "登录成功"}
    raise HTTPException(401, "密码错误")


@router.post("/problems")
async def admin_list_problems(body: AdminPasswordRequest = AdminPasswordRequest()):
    """List all problems with full details."""
    if not _verify_admin(body.model_dump()):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_all_problem_ids, get_problems_by_ids

    ids = get_all_problem_ids()
    problems = get_problems_by_ids(ids)
    result = []
    for p in problems:
            result.append(AdminProblemOut(
                id=p.id, title=p.title, topic=p.topic,
                difficulty=p.difficulty, description=p.description,
                visible_test_cases_list=p.visible_test_cases,
                test_cases_list=p.test_cases,
                brute_solution=p.brute_solution,
                optimal_solution=p.optimal_solution,
                starter_code=p.starter_code,
                function_signature=p.function_signature,
                time_complexity=p.time_complexity,
                space_complexity=p.space_complexity,
                source=p.source,
                source_url=p.source_url,
                constraints=p.constraints,
                alternative_solutions=p.alternative_solutions_list,
                novelty_score=p.novelty_score,
                created_at=p.created_at,
            ))
    return {"problems": [p.model_dump(by_alias=True) for p in result]}


@router.put("/problem/{problem_id}")
async def admin_update_problem(problem_id: int, body: AdminUpdateProblemRequest):
    """Update a problem. Only provided fields are updated."""
    admin_body = body if isinstance(body, dict) else body.model_dump(exclude_none=True)
    if not _verify_admin(admin_body):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_problem_by_id

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    updates = []
    params = []
    if body.title is not None:
        updates.append("title = ?"); params.append(body.title)
    if body.description is not None:
        updates.append("description = ?"); params.append(body.description)
    if body.topic is not None:
        updates.append("topic = ?"); params.append(body.topic)
    if body.difficulty is not None:
        updates.append("difficulty = ?"); params.append(body.difficulty)
    if body.test_cases is not None:
        updates.append("test_cases_json = ?"); params.append(json.dumps(body.test_cases, ensure_ascii=False))
    if body.visible_test_cases is not None:
        updates.append("visible_test_cases_json = ?"); params.append(json.dumps(body.visible_test_cases, ensure_ascii=False))
    if body.brute_solution is not None:
        updates.append("brute_solution = ?"); params.append(body.brute_solution)
    if body.starter_code is not None:
        updates.append("starter_code = ?"); params.append(body.starter_code)
    if body.novelty_score is not None:
        updates.append("novelty_score = ?"); params.append(body.novelty_score)
    if body.function_signature is not None:
        updates.append("function_signature = ?"); params.append(body.function_signature)
    if body.time_complexity is not None:
        updates.append("time_complexity = ?"); params.append(body.time_complexity)
    if body.space_complexity is not None:
        updates.append("space_complexity = ?"); params.append(body.space_complexity)
    if body.source is not None:
        updates.append("source = ?"); params.append(body.source)
    if body.source_url is not None:
        updates.append("source_url = ?"); params.append(body.source_url)
    if body.optimal_solution is not None:
        updates.append("optimal_solution = ?"); params.append(body.optimal_solution)
    if body.constraints is not None:
        import json
        updates.append("constraints_json = ?"); params.append(json.dumps(body.constraints, ensure_ascii=False))
    if body.alternative_solutions is not None:
        import json
        updates.append("alternative_solutions = ?"); params.append(json.dumps(body.alternative_solutions, ensure_ascii=False))

    if body.test_cases is not None and body.visible_test_cases is None:
        derived_visible = [tc for tc in body.test_cases if not tc.get("is_hidden", False)]
        if derived_visible:
            updates.append("visible_test_cases_json = ?"); params.append(json.dumps(derived_visible, ensure_ascii=False))

    if updates:
        params.append(problem_id)
        from code_tutor_agent.db.database import _get_conn
        conn = _get_conn()
        conn.execute(f"UPDATE problems SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        conn.close()

    return {"ok": True, "message": f"Problem {problem_id} updated"}


@router.post("/problem/{problem_id}/delete")
async def admin_delete_problem(problem_id: int, body: AdminPasswordRequest = AdminPasswordRequest()):
    """Delete a problem."""
    if not _verify_admin(body.model_dump()):
        raise HTTPException(401, "密码错误")

    from code_tutor_agent.db.database import get_problem_by_id, _get_conn

    full = get_problem_by_id(problem_id)
    if not full:
        raise HTTPException(404, f"Problem {problem_id} not found")

    conn = _get_conn()
    conn.execute("DELETE FROM submissions WHERE problem_id = ?", (problem_id,))
    conn.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
    conn.commit()
    conn.close()

    return {"ok": True, "message": f"Problem {problem_id} deleted"}


@router.get("/profile")
async def admin_get_profile():
    """Get the current user profile."""
    from code_tutor_agent.db.database import get_profile
    return get_profile().model_dump()