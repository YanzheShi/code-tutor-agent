"""LeetCode router — /leetcode/parse endpoint."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException

from code_tutor_agent.schemas.api import LeetCodeParseRequest, LeetCodeParseResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/leetcode/parse")
async def parse_leetcode(body: LeetCodeParseRequest):
    """Parse a LeetCode problem URL via GraphQL."""
    from code_tutor_agent.leetcode.leetcode_fetcher import fetch_problem, problem_to_api_dict

    url = body.url.strip().rstrip("/")
    logger.info("POST /leetcode/parse url=%s", url)

    match = re.search(r"/problems/([^/]+)", url)
    if not match:
        raise HTTPException(400, "无效的 LeetCode 链接，请粘贴完整题目 URL")

    slug = match.group(1)
    domain = "leetcode.cn" if ".cn" in url else "leetcode.com"

    try:
        p = fetch_problem(slug, domain=domain)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"获取题目失败: {exc}")

    if not p.title:
        raise HTTPException(404, f"Problem '{slug}' 未在 LeetCode 找到")

    data = problem_to_api_dict(p)
    return LeetCodeParseResponse(**data)