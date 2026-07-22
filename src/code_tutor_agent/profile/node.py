"""规划 Agent 专属 writer —— 消费 profile_delta，写 store。

⚠️ 单 writer 纪律：只有这一个 node 调 store.put()。
判题/辅导哪怕想改画像，也只能挂 state["profile_delta"]。
"""
from __future__ import annotations

import logging
import time

from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore

from .schema import ProfileDelta, UserProfile
from .scoring import apply_delta
from code_tutor_agent.schemas.state import SessionState

STORE_NS = ("user_profiles",)
DEFAULT_USER_ID = "default"


def _empty_profile() -> UserProfile:
    return {
        "prof": {},
        "prof_elo_raw": {},
        "stab": {},
        "forget": {},
        "errors": {"_global": {}, "per_tag": {}},
        "attempts": {},
        "meta": {"schema_version": "mvp@1", "updated_at": 0.0},
    }


def update_profile_node(
    state: SessionState,
    *,
    store: BaseStore,
    config: RunnableConfig,
) -> dict:
    """消费 session_state.profile_delta，写 store。"""
    delta: dict | None = state.profile_delta
    if not delta:
        return {}

    problem_id = state.problem.problem_id if state.problem else 0
    code_hash = None
    user_id = config.get("configurable", {}).get("user_id", DEFAULT_USER_ID)

    item = store.get(STORE_NS, user_id)
    profile: UserProfile = item.value if item else _empty_profile()

    updated = apply_delta(
        profile=profile,
        delta=delta,
        problem_id=problem_id,
        code_hash=code_hash,
        now=time.time(),
    )

    store.put(STORE_NS, user_id, updated)
    # 也写一份到 SQLite 供前端展示
    try:
        from code_tutor_agent.db.database import save_user_profile_v2
        import json as _json
        save_user_profile_v2(updated)
    except Exception:
        logger.warning("Failed to persist profile to SQLite (non-fatal)", exc_info=True)

    # ── 路由 ──
    # 常规模式：graph.py 已有静态边 update_profile_node → critic_node，直接 return {} 即可，
    #   切勿 return Command(goto="critic_node")，否则会与静态边冲突。
    # agent 模式：静态边会误导向 critic_node，必须显式 Command(goto=agent_tutor_node) 覆盖。
    if state.mode == "agent":
        from langgraph.types import Command
        return Command(update={}, goto="agent_tutor_node")
    return {}