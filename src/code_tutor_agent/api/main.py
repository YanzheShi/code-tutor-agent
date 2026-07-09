"""FastAPI application — CodeTutor Agent HTTP entry point.

Responsibilities:
    - Create the FastAPI app
    - Register all routers (business logic lives in ``api/routers/``)
    - Start/stop lifecycle (compile LangGraph)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from code_tutor_agent.api.deps import init_graph
from code_tutor_agent.api.routers import (
    admin,
    chat,
    leetcode,
    problems,
    run,
    session,
)
from code_tutor_agent.progress import _generation_progress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: compile the LangGraph once."""
    init_graph()
    _generation_progress.clear()
    yield


app = FastAPI(
    title="CodeTutor Agent",
    version="0.1.0",
    description="AI-powered coding tutor with multi-agent architecture",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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