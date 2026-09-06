"""Admin token 成本统计路由（鉴权已上移：main.py 路由级 require_admin）。

端点(均挂在 /admin/token 下):
- POST /overview  → KPI + 趋势 + 模块成本占比 + Top5
- POST /purposes  → 按业务用途统计(含环比)
- POST /cache     → 各用途缓存命中率 + 失效诊断
- POST /budget    → 预算使用 + 预警事件(单用户)
- POST /usage     → 调用明细(token_usage 明细表)
- GET  /usage.csv → 明细 CSV 导出

多用户改造（2026-09-06）：旧的 body 密码校验废弃，改 JWT require_admin；
token_usage 已带 user_id，可按用户归集成本。
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse

from code_tutor_agent.db import database
from code_tutor_agent.schemas.api import TokenStatsRequest

logger = logging.getLogger(__name__)
router = APIRouter()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_dates(body: TokenStatsRequest) -> tuple[str | None, str | None]:
    """校验日期格式,非法则忽略(返回 None)。"""
    f = body.from_date if body.from_date and _DATE_RE.match(body.from_date) else None
    t = body.to_date if body.to_date and _DATE_RE.match(body.to_date) else None
    return f, t


@router.post("/overview")
async def token_overview(body: TokenStatsRequest):
    f, t = _clean_dates(body)
    return database.query_token_overview(f, t, body.model_alias)


@router.post("/purposes")
async def token_purposes(body: TokenStatsRequest):
    f, t = _clean_dates(body)
    return {"rows": database.query_token_purposes(f, t, body.model_alias)}


@router.post("/cache")
async def token_cache(body: TokenStatsRequest):
    f, t = _clean_dates(body)
    return {"rows": database.query_token_cache(f, t, body.model_alias)}


@router.post("/budget")
async def token_budget(body: TokenStatsRequest):
    return database.query_token_budget()


@router.post("/usage")
async def token_usage(body: TokenStatsRequest):
    f, t = _clean_dates(body)
    limit = max(1, min(int(body.limit or 100), 5000))
    return {"rows": database.query_token_usage_recent(limit=limit, from_date=f, to_date=t)}


@router.post("/usage/export")
async def token_usage_export(body: TokenStatsRequest):
    """导出调用明细 CSV。返回 text/csv 附件供前端触发下载。"""
    f, t = _clean_dates(body)
    limit = max(1, min(int(body.limit or 5000), 5000))
    csv_text = database.export_token_usage_csv(from_date=f, to_date=t, limit=limit)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=token_usage.csv"},
    )
