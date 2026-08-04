"""规划 Agent 专属 writer —— 消费 profile_delta，写 store。

⚠️ 单 writer 纪律：只有这一个 node 调 store.put()。
判题/辅导哪怕想改画像，也只能挂 state["profile_delta"]。
"""
from __future__ import annotations

import logging
import time
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore
from langgraph.types import Command

from .schema import ProfileDelta, UserProfile
from .scoring import apply_delta
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)

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
) -> Command[Literal["critic_node"]]:
    """消费 session_state.profile_delta，写 store，然后统一路由到 critic_node。

    路由说明（2026-08-04 修复）：
    langgraph 1.2.7 中 Command(goto) **不会覆盖**静态边——两者同时生效
    （见 scripts/verify_command_edge_conflict.py）。历史上本节点有一条
    update_profile_node → critic_node 的静态边，agent 模式又返回
    Command(goto="agent_tutor_node") 试图"覆盖"它，实际造成 critic_node 与
    agent_tutor_node 并行双执行。现静态边已移除，两种模式统一经
    Command(goto="critic_node") 路由；agent 模式 AC 的收尾（flush 历史 +
    phase=reviewing + 暂停在 wait_for_submit）由 critic_node 的 AC 分支完成，
    status="done" 由 agent_judge_node 的 AC 分支写入。
    """
    delta: dict | None = state.profile_delta
    if delta:
        problem_id = state.problem.problem_id if state.problem else 0
        code_hash = None
        user_id = config.get("configurable", {}).get("user_id", DEFAULT_USER_ID)

        item = store.get(STORE_NS, user_id)
        if item:
            profile: UserProfile = item.value
        else:
            # InMemoryStore 为空（服务器重启后），从 SQLite 兜底恢复
            profile = _empty_profile()
            try:
                from code_tutor_agent.db.database import get_user_profile_v2
                sqlite_profile = get_user_profile_v2()
                if sqlite_profile.get("prof"):
                    profile = sqlite_profile
                    store.put(STORE_NS, user_id, profile)
                    logger.info("Profile restored from SQLite after restart")
            except Exception:
                pass

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
            save_user_profile_v2(updated)
        except Exception:
            logger.warning("Failed to persist profile to SQLite (non-fatal)", exc_info=True)

    # 无 delta 也要路由（保证链路不断）；有 delta 时上方已完成写入
    return Command(goto="critic_node")