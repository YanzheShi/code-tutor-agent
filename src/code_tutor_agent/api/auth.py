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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from code_tutor_agent.db.database import (
    consume_password_reset_code,
    create_user,
    get_user_by_email,
    get_user_by_id,
    save_password_reset_code,
    update_user_password,
    verify_password_reset_code,
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


# ── IP 限流（内存滑动窗口；防脚本批量注册 / 穷举邀请码 / 轰炸找回接口）──

_RATE_BUCKETS: dict[str, list[float]] = {}


def rate_limit(key: str, max_requests: int, window_sec: float) -> None:
    """超限抛 429；内存态，重启清零（本应用单进程部署，够用）。"""
    now = time.monotonic()
    bucket = _RATE_BUCKETS.setdefault(key, [])
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_requests:
        raise HTTPException(429, "操作过于频繁，请稍后再试")
    bucket.append(now)


def _client_ip(request: Request | None) -> str:
    """取客户端 IP（X-Forwarded-For 由 nginx 设置时取第一跳）；单测直调无 Request → "direct"。"""
    if request is None:
        return "direct"
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱（登录账号）")
    password: str = Field(min_length=8, description="密码（至少 8 位）")
    invite_code: str = Field(description="邀请码（admin 面板生成，额度内有效）")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(description="当前密码")
    new_password: str = Field(min_length=8, description="新密码（至少 8 位）")


class ForgotPasswordRequest(BaseModel):
    email: str = Field(description="注册邮箱")


class ResetPasswordRequest(BaseModel):
    email: str = Field(description="注册邮箱")
    code: str = Field(description="邮箱收到的验证码")
    new_password: str = Field(min_length=8, description="新密码（至少 8 位）")


class LoginRequest(BaseModel):
    email: str = Field(description="邮箱")
    password: str = Field(description="密码")


class AuthResponse(BaseModel):
    token: str
    user: dict


def _user_payload(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest, request: Request = None):
    """邀请码注册：无有效码不能注册（额度/有效期/停用任一不满足即拒）。

    注册用户一律 role=user（管理员由启动脚本分配，见 ensure_bootstrap_admin）。
    """
    rate_limit(f"reg:{_client_ip(request)}", 5, 3600)  # 每 IP 每小时 5 次注册
    email = body.email.strip().lower()
    code = body.invite_code.strip().upper()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(body.password) < 8:
        raise HTTPException(400, "密码至少 8 位")

    if get_user_by_email(email):
        raise HTTPException(409, "该邮箱已注册")

    # 先扣额度（原子），后建号；建号失败属极端情况，额度已扣记日志即可
    from code_tutor_agent.db.database import consume_invite_code
    if not consume_invite_code(code):
        raise HTTPException(400, "邀请码无效或已用完")

    try:
        uid = create_user(email, hash_password(body.password), role="user")
    except Exception as exc:
        # 并发注册撞 UNIQUE → 409；其余转 500
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "该邮箱已注册")
        logger.exception("register failed (invite %s consumed)", code)
        raise HTTPException(500, "注册失败，请稍后重试")

    user = get_user_by_id(uid)
    logger.info("user registered: id=%s email=%s (invite=%s)", uid, email, code)
    return {"token": create_access_token(user), "user": _user_payload(user)}


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request):
    rate_limit(f"login:{_client_ip(request)}", 10, 600)  # 每 IP 10 分钟 10 次
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


@router.post("/me/password")
async def change_my_password(body: ChangePasswordRequest, current: dict = Depends(get_current_user)):
    """自助改密：验证旧密码后更新（忘记密码走 /forgot-password 或找管理员）。"""
    user = get_user_by_id(current["id"])
    if not user or not verify_password(body.old_password, user["password_hash"]):
        raise HTTPException(400, "当前密码不正确")
    if not update_user_password(current["id"], hash_password(body.new_password)):
        raise HTTPException(500, "修改失败，请稍后重试")
    logger.info("password changed: user=%s", current["id"])
    return {"ok": True}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    """忘记密码：配置 BREVO_API_KEY 时发 6 位验证码邮件；未配置返回引导信息。

    无论邮箱是否存在一律 200 + 统一措辞（防账号枚举探测）。
    """
    rate_limit(f"forgot:{_client_ip(request)}", 3, 3600)
    email = body.email.strip().lower()
    generic = {"delivered": None, "message": "如果该邮箱已注册，验证码将在几分钟内送达；请查收（含垃圾箱）。"}

    from code_tutor_agent.api import email as email_svc
    if not email_svc.is_configured():
        return {"delivered": False, "message": "邮件服务未配置，请联系管理员在后台为您重置密码。"}

    user = get_user_by_email(email)
    if not user:
        return generic  # 不泄露邮箱是否存在

    code = "".join(secrets.choice("23456789") for _ in range(6))  # 去掉易混 0/1
    code_hash = hashlib.sha256((code + _get_jwt_secret()).encode("utf-8")).hexdigest()
    from datetime import datetime as _dt
    expires = (_dt.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    save_password_reset_code(email, code_hash, expires)
    sent = email_svc.send_email(
        email,
        "Code Tutor 密码重置验证码",
        f"您的密码重置验证码是：{code}\n\n15 分钟内有效。如果不是您本人操作，请忽略本邮件。",
    )
    return {**generic, "delivered": sent}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    """用邮箱验证码重置密码（ Brevo 已配置时开放）。"""
    rate_limit(f"reset:{_client_ip(request)}", 5, 3600)
    email = body.email.strip().lower()
    code_hash = hashlib.sha256((body.code.strip() + _get_jwt_secret()).encode("utf-8")).hexdigest()
    user = get_user_by_email(email)
    if not user or not verify_password_reset_code(email, code_hash):
        raise HTTPException(400, "验证码错误或已过期")
    if not update_user_password(user["id"], hash_password(body.new_password)):
        raise HTTPException(500, "重置失败，请稍后重试")
    consume_password_reset_code(email, code_hash)
    logger.info("password reset via email code: user=%s", user["id"])
    return {"ok": True}
