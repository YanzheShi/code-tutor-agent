"""Admin router — management endpoints（鉴权已上移：main.py 路由级 require_admin）。

多用户改造（2026-09-06）：旧的明文密码 body 校验废弃，改用 JWT + role=admin。
`_verify_admin` 保留仅为兼容（旧测试/旧调用方引用）；端点内不再调用。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from code_tutor_agent.api.auth import require_admin, user_key
from code_tutor_agent.schemas.api import (
    AdminLoginRequest,
    AdminPasswordRequest,
    AdminProblemOut,
    AdminUpdateProblemRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

def _get_admin_password() -> str | None:
    # 不缓存:env 在进程启动时经 load_dotenv 注入,每次直读避免测试间状态污染
    return os.getenv("ADMIN_PASSWORD")


def _verify_admin(request_body: dict) -> bool:
    """兼容保留：实际鉴权已由路由级 require_admin 完成，此处恒放行。"""
    return True


@router.post("/login")
async def admin_login(body: AdminLoginRequest):
    """兼容旧前端的登录端点：真实鉴权已走 /auth/login + JWT，这里恒返回 ok。"""
    return {"ok": True, "message": "Admin mode (JWT role-based auth)"}


@router.post("/problems")
async def admin_list_problems(body: AdminPasswordRequest = AdminPasswordRequest()):
    """List all problems with full details."""
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
async def admin_get_profile(
    user_id: Optional[str] = None,
    current: dict = Depends(require_admin),
):
    """Get a user's profile (old 5-dim)。默认当前 admin 自己；?user_id= 可查任意用户。"""
    from code_tutor_agent.db.database import get_profile
    return get_profile(user_id or user_key(current))


@router.get("/profile/v2")
async def admin_get_profile_v2(
    user_id: Optional[str] = None,
    current: dict = Depends(require_admin),
):
    """Get a user's per-tag UserProfile。默认当前 admin 自己；?user_id= 可查任意用户。"""
    from code_tutor_agent.db.database import get_user_profile_v2
    return get_user_profile_v2(user_id or f"{user_key(current)}_v2")


@router.post("/submissions")
async def admin_list_submissions(body: AdminPasswordRequest = AdminPasswordRequest()):
    """List all recent submissions across all problems."""
    from code_tutor_agent.db.database import get_all_submissions
    return {"submissions": get_all_submissions()}


# ── 用户管理（多用户防滥用改造，2026-09-06）──

@router.get("/users")
async def admin_list_users(current: dict = Depends(require_admin)):
    """用户列表（不含密码哈希）。"""
    from code_tutor_agent.db.database import list_users
    return {"users": list_users()}


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, current: dict = Depends(require_admin)):
    """重置某用户密码为随机临时密码；明文只在本次响应返回一次，请立即发给用户。"""
    import secrets as _secrets

    from code_tutor_agent.api.auth import hash_password
    from code_tutor_agent.db.database import get_user_by_id, update_user_password

    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    temp_password = "".join(_secrets.choice(alphabet) for _ in range(10))
    if not update_user_password(user_id, hash_password(temp_password)):
        raise HTTPException(500, "重置失败，请稍后重试")
    logger.info("admin %s reset password of user %s", current["id"], user_id)
    return {"ok": True, "temp_password": temp_password, "email": user["email"]}


# ── 邀请码管理 ──

@router.get("/invites")
async def admin_list_invites(current: dict = Depends(require_admin)):
    """邀请码列表（含额度使用情况）。"""
    from code_tutor_agent.db.database import list_invite_codes
    return {"invites": list_invite_codes()}


class InviteCreateRequest(BaseModel):
    max_uses: int = 100
    expires_days: int = 1
    note: str = ""


@router.post("/invites")
async def admin_create_invite(body: InviteCreateRequest, current: dict = Depends(require_admin)):
    """生成邀请码：额度 + 有效天数自定（expires_days=0 表示永久）。"""
    import secrets as _secrets

    from datetime import datetime as _dt, timedelta as _td

    from code_tutor_agent.db.database import create_invite_code

    max_uses = max(1, min(int(body.max_uses), 10000))
    expires_at = None
    if body.expires_days > 0:
        expires_at = (_dt.now() + _td(days=body.expires_days)).strftime("%Y-%m-%d %H:%M:%S")
    # 去易混字符的 8 位码；主键冲突重试
    for _ in range(5):
        code = "".join(_secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        if create_invite_code(code, max_uses, expires_at, body.note):
            logger.info("admin %s created invite %s (max_uses=%s, expires=%s)",
                        current["id"], code, max_uses, expires_at or "never")
            return {"ok": True, "code": code, "max_uses": max_uses, "expires_at": expires_at}
    raise HTTPException(500, "邀请码生成失败，请重试")


@router.post("/invites/{code}/disable")
async def admin_disable_invite(code: str, current: dict = Depends(require_admin)):
    """停用邀请码（立即失效，额度不恢复）。"""
    from code_tutor_agent.db.database import set_invite_code_active
    if not set_invite_code_active(code.upper(), False):
        raise HTTPException(404, "邀请码不存在")
    return {"ok": True}
