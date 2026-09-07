"""监控告警与公告路由（docs/monitoring-alerts-design.md §9/§15）。

- admin_router（挂 /admin，require_admin）：指标快照 / 告警历史 / 测试邮件 / 公告管理
- public_router（挂根，登录即可读）：GET /announcements 主页横幅数据源
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from code_tutor_agent.api.auth import require_admin

logger = logging.getLogger(__name__)

admin_router = APIRouter()
public_router = APIRouter()
client_router = APIRouter()  # 客户端上报（公开：出错时 token 可能已失效）


# ── 指标 / 告警（admin） ─────────────────────────────────────

@admin_router.get("/metrics")
async def admin_metrics():
    """进程内指标快照（将来接 Prometheus 的 exposition 挂载点）。"""
    from code_tutor_agent.monitoring.metrics import get_registry

    snap = get_registry().snapshot()
    snap["alerts_enabled"] = True
    return snap


@admin_router.get("/alerts")
async def admin_alerts(limit: int = Query(default=50, ge=1, le=200)):
    """告警历史（倒序）。"""
    from code_tutor_agent.db.database import get_recent_alerts

    return {"alerts": get_recent_alerts(limit)}


@admin_router.post("/alerts/test")
async def admin_alert_test(current: dict = Depends(require_admin)):
    """发一封测试告警邮件，验证链路；返回实际送达情况。"""
    from code_tutor_agent.api.email import is_configured, send_email
    from code_tutor_agent.monitoring.notifier import _recipients

    if not is_configured():
        raise HTTPException(503, "mcp-hub 未配置（MCP_HUB_URL/MCP_HUB_TOKEN），邮件通道不可用")
    recipients = _recipients()
    if not recipients:
        raise HTTPException(503, "无收件人：请配置 CTA_ALERT_EMAIL_TO 或 CTA_ADMIN_EMAIL")
    ok = any(
        send_email(
            to,
            "[CodeTutor 测试] 监控告警链路验证",
            "这是一封测试邮件——收到即代表监控告警邮件链路正常。\n\n"
            f"触发人: {current.get('email') or current.get('user_id') or 'admin'}",
        )
        for to in recipients
    )
    if not ok:
        raise HTTPException(502, "测试邮件发送失败，看后端日志定位")
    return {"sent": True, "recipients": recipients}


# ── 公告（admin 管理） ──────────────────────────────────────

class AnnouncementCreateRequest(BaseModel):
    level: str = Field(default="info", pattern="^(info|warning|critical)$")
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(default="", max_length=2000)
    starts_at: str | None = None
    ends_at: str | None = None


@admin_router.get("/announcements")
async def admin_list_announcements():
    """当前生效中的公告（管理端复用用户侧口径；历史管理后续接入 admin 面板时扩展）。"""
    from code_tutor_agent.db.database import get_active_announcements

    return {"announcements": get_active_announcements()}


@admin_router.post("/announcements")
async def admin_create_announcement(body: AnnouncementCreateRequest):
    """发布一条手动公告。"""
    from code_tutor_agent.db.database import create_announcement

    ann_id = create_announcement(
        body.level, body.title, body.content, body.starts_at, body.ends_at
    )
    if not ann_id:
        raise HTTPException(500, "公告创建失败，看后端日志")
    logger.info("announcement created id=%d level=%s title=%s", ann_id, body.level, body.title)
    return {"id": ann_id}


@admin_router.post("/announcements/{announcement_id}/disable")
async def admin_disable_announcement(announcement_id: int):
    """下线公告。"""
    from code_tutor_agent.db.database import deactivate_announcement

    if not deactivate_announcement(announcement_id):
        raise HTTPException(404, f"公告 {announcement_id} 不存在")
    return {"disabled": True}


# ── 公告（用户侧） ──────────────────────────────────────────

@public_router.get("/announcements")
async def list_active_announcements():
    """主页横幅数据源：当前生效中的公告（登录用户可读，路由级鉴权在 main.py）。"""
    from code_tutor_agent.db.database import get_active_announcements

    return {"announcements": get_active_announcements()}


# ── 前端错误上报（设计 §14.2） ───────────────────────────────

# 字段白名单 + 限长（整包 ≤2KB，防滥用；超限静默丢弃不报 422——上报方是出错的前端）
_CLIENT_ERR_LIMITS = {"source": 64, "message": 500, "session_id": 64, "path": 200}


@client_router.post("/client/errors")
async def report_client_error(request: Request):
    """前端错误上报：进计数窗口（client_error_rate 规则用）+ WARNING 日志留痕。

    公开端点（无鉴权）：出错场景下 token 可能已失效。
    手动解析原始 body 且**不校验 Content-Type**——前端优先 sendBeacon + text/plain
    （跨域简单请求免 CORS 预检，页面卸载也不丢），也兼容 fetch + application/json。
    字段白名单 + 限长；任何解析失败返回 200 {"ok": false}，不让上报方再抛错。
    """
    import json as _json

    from code_tutor_agent.monitoring.metrics import get_registry

    get_registry().record("client_error")

    raw = await request.body()
    if not raw or len(raw) > 2048:
        return {"ok": False, "reason": "empty or oversized payload"}
    try:
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return {"ok": False, "reason": "not an object"}
    except Exception:
        return {"ok": False, "reason": "invalid json"}

    fields: dict[str, str] = {}
    for key, limit in _CLIENT_ERR_LIMITS.items():
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val.strip()[:limit]

    if not fields.get("source") or not fields.get("message"):
        return {"ok": False, "reason": "missing required fields"}

    logger.warning(
        "client error reported",
        extra={
            "client_source": fields["source"],
            "client_message": fields["message"],
            "client_session": fields.get("session_id", "-"),
            "client_path": fields.get("path", "-"),
        },
    )
    return {"ok": True}
