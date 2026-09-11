"""FastAPI application — CodeTutor Agent HTTP entry point.

Responsibilities:
    - Create the FastAPI app
    - Register all routers (business logic lives in ``api/routers/``)
    - Start/stop lifecycle (compile LangGraph)
    - Structured JSON logging with request_id tracing
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from code_tutor_agent.api.auth import get_current_user, require_admin, router as auth_router
from code_tutor_agent.api.deps import init_graph
from code_tutor_agent.api.logging_config import request_id_ctx, setup_logging
from code_tutor_agent.api.routers import (
    admin,
    chat,
    problems,
    run,
    session,
    settings,
    token,
)
from code_tutor_agent.api.routers.monitoring import admin_router as monitoring_admin_router
from code_tutor_agent.api.routers.monitoring import client_router as monitoring_client_router
from code_tutor_agent.api.routers.monitoring import public_router as monitoring_public_router
from code_tutor_agent.progress import _generation_progress

# ── 结构化 JSON 日志（必须在所有 logger 使用之前调用）──
setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: init DB schema, compile the LangGraph once, start background cleanup."""
    # 建表/迁移必须在任何请求之前完成：init_db 原先只在 save_problem/token sink
    # 懒触发，存量库启动后 users 等新表不存在 → /auth/register 直接 500（实测踩坑）。
    from code_tutor_agent.api.auth import ensure_bootstrap_admin
    from code_tutor_agent.db.database import init_db

    init_db()
    ensure_bootstrap_admin()

    init_graph()
    _generation_progress.clear()

    # 启动后台 TTL 清理任务
    cleanup_task = asyncio.create_task(_session_cleanup_loop())
    logger.info("Background session cleanup task started")

    # 启动监控告警 watcher（docs/monitoring-alerts-design.md §3 ③）
    monitor_task: asyncio.Task | None = None
    if os.getenv("CTA_ALERTS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
        from code_tutor_agent.monitoring.watcher import run_watch_loop

        monitor_task = asyncio.create_task(run_watch_loop())
        logger.info("Monitoring watcher task started")

    yield

    # Shutdown: 取消后台任务
    for task, name in ((cleanup_task, "cleanup"), (monitor_task, "monitor")):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Background %s task stopped", name)


app = FastAPI(
    title="CodeTutor Agent",
    version="0.1.0",
    description="AI-powered coding tutor with multi-agent architecture",
    lifespan=lifespan,
)

# ── CORS：允许的前端来源（CORS_ORIGINS 环境变量，逗号分隔）──
_cors_origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 监控埋点辅助（非侵入：失败静默，docs/monitoring-alerts-design.md §4.2）──
def _m_http(status_code: int) -> None:
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        reg = get_registry()
        reg.record("http_total")
        if status_code >= 500:
            reg.record("http_5xx")
    except Exception:
        pass


# ── 请求链路追踪：注入 request_id + 记录请求日志 ──
@app.middleware("http")
async def request_tracing_middleware(request: Request, call_next):
    """为每个 HTTP 请求注入 request_id，贯穿所有日志。

    - 如果请求头带有 X-Request-ID，则复用（方便跨服务追踪）
    - 否则自动生成一个
    - 响应头也携带 X-Request-ID，前端可以拿到问题排查 ID
    """
    from code_tutor_agent.api.logging_config import generate_request_id

    request_id = request.headers.get("X-Request-ID", generate_request_id())
    request_id_ctx.set(request_id)

    start = time.monotonic()
    try:
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        _m_http(response.status_code)
        logger.info("request completed", extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        })
        return response
    except Exception:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        _m_http(500)
        logger.exception("request failed", extra={
            "method": request.method,
            "path": request.url.path,
            "duration_ms": duration_ms,
        })
        raise

# ── 用户级 LLM 设置注入：请求进入时按 token 加载该用户自定义模型配置 ──
# 解 token 失败（未登录/过期）静默跳过 —— 鉴权由路由依赖负责，这里只做增强。
@app.middleware("http")
async def user_llm_settings_middleware(request: Request, call_next):
    from code_tutor_agent.api.routers.settings import load_user_llm_cfg
    from code_tutor_agent.runtime_settings import set_llm_override

    reset_token = None
    try:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                from code_tutor_agent.api.auth import decode_token
                uid = int(decode_token(auth[7:].strip()).get("sub", ""))
            except Exception:
                uid = 0
            if uid:
                cfg = load_user_llm_cfg(uid)
                if cfg:
                    reset_token = set_llm_override(cfg)
    except Exception:
        logger.debug("user llm settings injection skipped", exc_info=True)
    try:
        return await call_next(request)
    finally:
        if reset_token is not None:
            from code_tutor_agent.runtime_settings import llm_override_ctx
            llm_override_ctx.reset(reset_token)


# ── Register routers ──
# 多用户改造（2026-09-06）：/auth 开放；业务路由统一 Bearer 鉴权；
# admin/token 路由要求 admin 角色（替代旧明文密码 body 校验）。
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(settings.router, prefix="/settings", tags=["settings"],
                   dependencies=[Depends(get_current_user)])
app.include_router(session.router, prefix="/session", tags=["session"],
                   dependencies=[Depends(get_current_user)])
app.include_router(run.router, prefix="/session", tags=["run"],
                   dependencies=[Depends(get_current_user)])
app.include_router(chat.router, prefix="/session", tags=["chat"],
                   dependencies=[Depends(get_current_user)])
app.include_router(problems.router, tags=["problems"],
                   dependencies=[Depends(get_current_user)])
app.include_router(admin.router, prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])
app.include_router(token.router, prefix="/admin/token", tags=["token"],
                   dependencies=[Depends(require_admin)])
# 监控告警 / 公告（admin 部分要求管理员；公告读取登录即可）
app.include_router(monitoring_admin_router, prefix="/admin", tags=["monitoring"],
                   dependencies=[Depends(require_admin)])
app.include_router(monitoring_public_router, tags=["monitoring"],
                   dependencies=[Depends(get_current_user)])
# 前端错误上报：公开（出错时 token 可能已失效），字段白名单+限长防滥用
app.include_router(monitoring_client_router, tags=["monitoring"])


@app.get("/health")
async def health():
    from code_tutor_agent.api.deps import get_graph
    ready = False
    try:
        get_graph()
        ready = True
    except RuntimeError:
        pass
    return {"status": "ok", "graph_ready": ready}


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 抓取端点：返回标准 text/plain exposition 格式。

    免鉴权（Prometheus 走独立 scrape 配置）；prometheus_client 未安装时返回 503。
    生产环境应通过内网/独立 token 限制该端点可达性，避免泄露运营指标。
    """
    from code_tutor_agent.monitoring.metrics import PROMETHEUS_AVAILABLE

    if not PROMETHEUS_AVAILABLE:
        raise HTTPException(status_code=503, detail="prometheus_client 未安装")
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── 后台会话 TTL 清理 ──

async def _session_cleanup_loop():
    """后台定时任务：每隔一段时间扫描并清理过期会话。

    通过 session_activity 表判断每个会话的最后活跃时间，
    超过 TTL 的会话会被自动删除（checkpointer + activity 记录）。
    """
    from code_tutor_agent.config import get_session_ttl_hours, get_cleanup_interval_minutes
    from code_tutor_agent.db.database import (
        delete_session_activity,
        delete_session_sidecar_data,
        get_stale_sessions,
    )

    interval_min = get_cleanup_interval_minutes()
    ttl_hours = get_session_ttl_hours()

    # 启动后先等 5 分钟再首次扫描，给 graph 初始化留时间
    await asyncio.sleep(300)

    while True:
        try:
            stale = get_stale_sessions(ttl_hours)
            if stale:
                logger.info("Auto-cleanup: found %d stale sessions (TTL=%dh)", len(stale), ttl_hours)
                try:
                    from code_tutor_agent.api.deps import get_graph
                    graph = get_graph()
                    checkpointer = graph.checkpointer
                except RuntimeError:
                    logger.warning("Auto-cleanup: graph not ready, skipping")
                    continue

                cleaned = 0
                for tid in stale:
                    try:
                        if hasattr(checkpointer, "delete_thread"):
                            checkpointer.delete_thread(tid)
                        delete_session_activity(tid)
                        delete_session_sidecar_data(tid)
                        _generation_progress.pop(tid, None)
                        cleaned += 1
                    except Exception as exc:
                        logger.warning("Auto-cleanup: failed to delete %s: %s", tid, exc)

                logger.info("Auto-cleanup: deleted %d/%d stale sessions", cleaned, len(stale))
            else:
                logger.debug("Auto-cleanup: no stale sessions")

        except Exception as exc:
            logger.exception("Auto-cleanup loop error: %s", exc)

        await asyncio.sleep(interval_min * 60)