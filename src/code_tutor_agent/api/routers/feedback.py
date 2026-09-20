"""用户反馈路由（docs/feedback-feature-plan.md）。

- ``user_router``（挂 /feedback，登录即可，**含 role='test' 体验账号**）：提交反馈
- ``admin_router``（挂 /admin/feedback，require_admin）：**只读**列表

口径（2026-09-20 决策，详见 plan）：管理端**只读** —— 无 status 流转、无处理备注、
无删除/编辑端点。反馈落库后即永久留档，管理端只负责查看与分诊。

⚠️ 本模块的 ``Request`` 必须**真实 import**：``from __future__ import annotations``
会把注解字符串化，FastAPI 运行期解析失败时**静默注入 None**（不报任何错）——
曾导致 session.py 的 IP 维度线上从未生效。限频依赖 ``_client_ip(request)``，
故 Phase 5 配了走真实路由的回归用例（tests/test_feedback.py）锁死该行为。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from code_tutor_agent.api.auth import (
    _client_ip,
    get_current_user,
    rate_limit,
    require_admin,
)

logger = logging.getLogger(__name__)

user_router = APIRouter()
admin_router = APIRouter()

# 分类白名单（与前端 FeedbackModal 的四个 chip 一一对应）
CATEGORIES = ("bug", "experience", "content", "other")

# 限频：复用 auth.rate_limit 的进程内桶（重启清零；conftest autouse 已做测试隔离）。
# 用户维度防个人刷屏，IP 维度防同一出口（教室/机房 NAT）批量灌表。
_RATE_USER = 10          # 次 / 小时 / 用户
_RATE_IP = 30            # 次 / 小时 / IP
_RATE_WINDOW = 3600.0

_CONTENT_MIN = 5
_CONTENT_MAX = 2000


class FeedbackCreateRequest(BaseModel):
    """提交反馈的请求体（服务端补齐身份与快照字段，客户端不可指定）。"""

    category: str = Field(default="other", pattern="^(bug|experience|content|other)$")
    content: str = Field(min_length=_CONTENT_MIN, max_length=_CONTENT_MAX)
    contact: str | None = Field(default=None, max_length=120)
    screen: str | None = Field(default=None, max_length=32)
    problem_id: int | None = None
    session_id: str | None = Field(default=None, max_length=64)

    @field_validator("content")
    @classmethod
    def _strip_content(cls, v: str) -> str:
        """先 strip 再判长度：纯空白串过得了 Field 的 min_length，必须在这里堵掉。"""
        v = v.strip()
        if len(v) < _CONTENT_MIN:
            raise ValueError(f"反馈内容至少 {_CONTENT_MIN} 个字")
        return v


@user_router.post("/feedback")
async def submit_feedback(
    body: FeedbackCreateRequest,
    request: Request,
    current: dict = Depends(get_current_user),
):
    """提交一条反馈。登录即可（体验账号同权），落库后管理端可读。"""
    from code_tutor_agent.db.database import create_feedback

    uid = current["id"]
    ip = _client_ip(request)

    # 先限频再落库：超限直接 429，不写库
    rate_limit(f"feedback:uid:{uid}", _RATE_USER, _RATE_WINDOW)
    rate_limit(f"feedback:ip:{ip}", _RATE_IP, _RATE_WINDOW)

    fid = create_feedback(
        user_id=uid,
        user_email=current.get("email") or "",
        category=body.category,
        content=body.content,
        contact=body.contact,
        screen=body.screen,
        problem_id=body.problem_id,
        session_id=body.session_id,
        user_agent=(request.headers.get("user-agent") or "")[:200],
    )
    if not fid:
        raise HTTPException(500, "反馈提交失败，请稍后再试")

    # 埋点：Prometheus cta_feedback_submitted_total（metrics 模块内部全程 try/except，不会抛）
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        get_registry().record("feedback_submitted")
    except Exception:
        pass

    logger.info(
        "feedback submitted id=%d uid=%s category=%s screen=%s",
        fid, uid, body.category, body.screen or "-",
    )
    return {"ok": True, "id": fid}


@admin_router.get("/admin/feedback")
async def admin_list_feedback(
    category: str | None = Query(default=None, pattern="^(bug|experience|content|other)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """反馈列表（**只读**）。

    一次返回三样东西，省掉前端第二个请求：
      - items：当前页数据（按 id 倒序）
      - total：当前筛选下的总条数（算分页）
      - counts：各分类计数（筛选 tab 角标；始终是全量口径，不受 category 参数影响）
    """
    from code_tutor_agent.db.database import (
        count_feedback,
        count_feedback_by_category,
        list_feedback,
    )

    return {
        "items": list_feedback(category=category, limit=limit, offset=offset),
        "total": count_feedback(category=category),
        "counts": count_feedback_by_category(),
    }
