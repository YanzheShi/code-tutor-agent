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
                id=p["id"], title=p.get("title", ""), topic=p.get("topic", ""),
                difficulty=p.get("difficulty", ""), description=p.get("description", ""),
                visible_test_cases_list=p.get("visible_test_cases", []),
                test_cases_list=p.get("test_cases", []),
                brute_solution=p.get("brute_solution", ""),
                starter_code=p.get("starter_code", ""),
                novelty_score=p.get("novelty_score", 7.0),
                created_at=p.get("created_at", ""),
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