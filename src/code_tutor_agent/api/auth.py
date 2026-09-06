"""认证与鉴权 — 邮箱密码注册/登录 + JWT + FastAPI 依赖。

设计口径（多用户改造，2026-09-06）：
- 开放注册，注册用户一律 role=user；管理员由启动时引导脚本直接分配（见
  ensure_bootstrap_admin：ADMIN_EMAIL / ADMIN_PASSWORD 环境变量，可覆盖）。
- 密码哈希用标准库 PBKDF2-HMAC-SHA256（390k 迭代，OWASP 推荐），零新增依赖。
- JWT 用 PyJWT（venv 已有传递依赖），HS256，有效期默认 7 天（JWT_EXPIRE_DAYS 可调）。
- JWT secret：优先 JWT_SECRET 环境变量；否则首次生成并持久化到 data/db/.jwt_secret，
  重启后 token 不失效。
- get_current_user 每次回库查用户（role 变更/禁用即时生效），不只信 token claims。

用户身份字符串（贯穿 profiles / session_activity / submissions / token_usage）：
    str(user.id)，如 "3"。v2 画像 key 约定为 f"{uid}_v2"（镜像旧 "default"/"default_v2"）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from code_tutor_agent.db.database import (
    create_user,
    get_user_by_email,
    get_user_by_id,
)

logger = logging.getLogger(__name__)

# ── 密码哈希（PBKDF2-HMAC-SHA256）──

_PBKDF2_ITERATIONS = 390_000
_HASH_NAME = "pbkdf2_sha256"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_HASH_NAME}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；格式不符（历史脏数据）一律 False。"""
    try:
        name, iterations, salt_hex, dk_hex = stored.split("$")
        if name != _HASH_NAME:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations),
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


# ── JWT ──

def _get_jwt_secret() -> str:
    """JWT secret：环境变量优先，否则持久化随机密钥到 data/db/.jwt_secret。"""
    from code_tutor_agent.db.database import DB_PATH

    secret = os.getenv("JWT_SECRET", "").strip()
    if secret:
        return secret

    secret_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".jwt_secret")
    try:
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as f:
                cached = f.read().strip()
            if cached:
                return cached
        os.makedirs(os.path.dirname(secret_path), exist_ok=True)
        generated = secrets.token_hex(32)
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write(generated)
        logger.info("JWT secret generated and persisted to %s", secret_path)
        return generated
    except OSError as exc:
        # 文件系统不可写时退化为进程内存生密钥（重启后 token 失效，可接受）
        logger.warning("JWT secret file unavailable (%s), using ephemeral secret", exc)
        return secrets.token_hex(32)


def _jwt_expire_days() -> int:
    try:
        return max(1, int(os.getenv("JWT_EXPIRE_DAYS", "7")))
    except ValueError:
        return 7


def create_access_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user.get("role", "user"),
        "iat": now,
        "exp": now + timedelta(days=_jwt_expire_days()),
    }
    return pyjwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict:
    """解码并校验 JWT；失败抛 jwt.PyJWTError。"""
    return pyjwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])


# ── FastAPI 依赖 ──

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """业务路由统一鉴权依赖：Bearer token → 回库查用户。

    回库而非只信 claims：角色变更即时生效；用户被删后 token 立即失效。
    """
    if creds is None or not creds.credentials:
        raise HTTPException(401, "未登录（缺少 Bearer token）")
    try:
        payload = decode_token(creds.credentials)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "登录已过期，请重新登录")
    except pyjwt.PyJWTError:
        raise HTTPException(401, "无效的登录凭证")
    try:
        uid = int(payload.get("sub", ""))
    except ValueError:
        raise HTTPException(401, "无效的登录凭证")
    user = get_user_by_id(uid)
    if not user:
        raise HTTPException(401, "用户不存在或已删除")
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


def require_admin(current: dict = Depends(get_current_user)) -> dict:
    """admin 路由守卫：替代旧的明文密码 body 校验。"""
    if current.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return current


def user_key(current: dict) -> str:
    """业务数据归属 key（profiles/session_activity/submissions 统一用）。"""
    return str(current["id"])


def profile_v2_key(current: dict) -> str:
    """v2 per-tag 画像的 profiles.user_id key（镜像旧 default/default_v2 约定）。"""
    return f"{current['id']}_v2"


def ensure_bootstrap_admin() -> None:
    """服务器启动时幂等分配管理员账号（工业化落地：管理员不靠前端注册产生）。

    - 账号来自 CTA_ADMIN_EMAIL / CTA_ADMIN_PASSWORD 环境变量，未配置时用内置默认值。
      ⚠️ 刻意不用 ADMIN_PASSWORD——那是旧 admin 明文密码流程的变量名，.env 里还留着，
      撞名会把引导密码读错（2026-09-06 实测踩坑）。
    - 目标邮箱已存在 → 什么都不做（幂等，重启不覆盖密码）；
    - 创建成功只打一行日志。
    """
    email = os.getenv("CTA_ADMIN_EMAIL", "534629255@qq.com").strip().lower()
    password = os.getenv("CTA_ADMIN_PASSWORD", "test123456")
    if get_user_by_email(email):
        return
    try:
        uid = create_user(email, hash_password(password), role="admin")
        logger.info("bootstrap admin created: id=%s email=%s", uid, email)
    except Exception as exc:
        logger.error("bootstrap admin creation failed: %s", exc)


# ── 路由 ──

router = APIRouter()


class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱（登录账号）")
    password: str = Field(min_length=8, description="密码（至少 8 位）")


class LoginRequest(BaseModel):
    email: str = Field(description="邮箱")
    password: str = Field(description="密码")


class AuthResponse(BaseModel):
    token: str
    user: dict


def _user_payload(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest):
    """开放注册：注册用户一律 role=user（管理员由启动脚本分配，见 ensure_bootstrap_admin）。"""
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(body.password) < 8:
        raise HTTPException(400, "密码至少 8 位")

    if get_user_by_email(email):
        raise HTTPException(409, "该邮箱已注册")

    try:
        uid = create_user(email, hash_password(body.password), role="user")
    except Exception as exc:
        # 并发注册撞 UNIQUE → 409；其余转 500
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "该邮箱已注册")
        logger.exception("register failed")
        raise HTTPException(500, "注册失败，请稍后重试")

    user = get_user_by_id(uid)
    logger.info("user registered: id=%s email=%s", uid, email)
    return {"token": create_access_token(user), "user": _user_payload(user)}


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest):
    email = body.email.strip().lower()
    user = get_user_by_email(email)
    # 统一 401，不区分「邮箱不存在」与「密码错误」（防账号枚举）
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "邮箱或密码错误")
    return {"token": create_access_token(user), "user": _user_payload(user)}


@router.get("/me")
async def me(current: dict = Depends(get_current_user)):
    """返回当前登录用户信息（前端启动时校验 token 有效性）。"""
    return {"user": current, "ts": int(time.time())}


@router.get("/me/profile")
async def my_profile(current: dict = Depends(get_current_user)):
    """当前用户的 v1 五维画像（前端「我的画像」Tab，普通用户可用）。"""
    from code_tutor_agent.db.database import get_profile
    return get_profile(user_key(current))


@router.get("/me/profile/v2")
async def my_profile_v2(current: dict = Depends(get_current_user)):
    """当前用户的 v2 per-tag 画像。"""
    from code_tutor_agent.db.database import get_user_profile_v2
    return get_user_profile_v2(profile_v2_key(current))
