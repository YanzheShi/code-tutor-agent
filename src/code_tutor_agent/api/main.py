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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from code_tutor_agent.api.deps import init_graph
from code_tutor_agent.api.logging_config import request_id_ctx, setup_logging
from code_tutor_agent.api.routers import (
    admin,
    chat,
    leetcode,
    problems,
    run,
    session,
)
from code_tutor_agent.progress import _generation_progress

# ── 结构化 JSON 日志（必须在所有 logger 使用之前调用）──
setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: compile the LangGraph once, start background cleanup."""
    init_graph()
    _generation_progress.clear()

    # 启动后台 TTL 清理任务
    cleanup_task = asyncio.create_task(_session_cleanup_loop())
    logger.info("Background session cleanup task started")

    yield

    # Shutdown: 取消清理任务
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Background session cleanup task stopped")


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
        "http://localhost:5173,http://127.0.0.1:5173",
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
        logger.info("request completed", extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        })
        return response
    except Exception:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        logger.exception("request failed", extra={
            "method": request.method,
            "path": request.url.path,
            "duration_ms": duration_ms,
        })
        raise

# ── Register routers ──
app.include_router(session.router, prefix="/session", tags=["session"])
app.include_router(run.router, prefix="/session", tags=["run"])
app.include_router(chat.router, prefix="/session", tags=["chat"])
app.include_router(problems.router, tags=["problems"])
app.include_router(leetcode.router, tags=["leetcode"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])


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


# ── 后台会话 TTL 清理 ──

async def _session_cleanup_loop():
    """后台定时任务：每隔一段时间扫描并清理过期会话。

    通过 session_activity 表判断每个会话的最后活跃时间，
    超过 TTL 的会话会被自动删除（checkpointer + activity 记录）。
    """
    from code_tutor_agent.config import get_session_ttl_hours, get_cleanup_interval_minutes
    from code_tutor_agent.db.database import get_stale_sessions, delete_session_activity

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