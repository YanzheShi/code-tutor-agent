"""自建搜索 MCP Server 客户端 — Streamable HTTP 传输 + Bearer 鉴权。

服务端契约（由服务方提供）：
- 端点 ``POST /mcp``，MCP Streamable HTTP（JSON-RPC 2.0，响应为 SSE ``data:`` 帧）
- 鉴权 ``Authorization: Bearer <token>``，每个应用一把
- 健康检查 ``GET /healthz`` → ``{"status":"ok"}``，**不需要** token
- 协议版本 ``2025-06-18``（由 SDK 在 ``initialize`` 中协商）

``Content-Type: application/json`` 与 ``Accept: application/json, text/event-stream``
由 ``mcp`` SDK 的 transport 自动注入，这里只补 ``Authorization``。

会话策略：**每次调用开一条短会话**（initialize → tool/call → DELETE）。
Long-lived session 需要把 anyio task group 的上下文跨 FastAPI 请求持有，
而 cancel scope 有任务亲和性，容易在异步服务里踩坑；搜索工具单次会话多一次
initialize 往返，换来的是无状态、无泄漏、并发天然隔离。

调用链路：
    agents/tools.py: search_* → call_search_tool(name, args)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8080/mcp"
DEFAULT_TIMEOUT_SECONDS = 20.0


class SearchMCPUnavailable(RuntimeError):
    """搜索 MCP 不可用（未配置 / 网络不可达 / 鉴权失败 / 服务端报错）。

    与「搜索没有结果」严格区分：前者是基础设施故障，导师应回退到自身知识
    作答；后者是正常业务结果，空结果也要照常返回。
    """


@dataclass
class SearchToolInfo:
    """MCP ``tools/list`` 里的一个工具。"""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


def _token() -> str:
    return (os.getenv("SEARCH_MCP_TOKEN") or "").strip()


def search_mcp_configured() -> bool:
    """是否配置了搜索 MCP 的 token。未配置时 agent 侧不注册 search 工具。"""
    return bool(_token())


def search_tool_name() -> str:
    """要调用的搜索工具名（MCP ``tools/list`` 里的 name）。

    默认 ``web_search``（自建网关契约）。对接其他搜索 MCP（如官方 Tavily
    的 ``tavily-search``）时，改 ``SEARCH_MCP_TOOL_NAME`` 即可，无需改代码。
    """
    return (os.getenv("SEARCH_MCP_TOOL_NAME") or "web_search").strip()


def _url() -> str:
    return (os.getenv("SEARCH_MCP_URL") or DEFAULT_URL).strip()


def _timeout() -> float:
    raw = os.getenv("SEARCH_MCP_TIMEOUT_SECONDS")
    try:
        return float(raw) if raw and raw.strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        logger.warning("SEARCH_MCP_TIMEOUT_SECONDS 非法(%r)，回退默认 %.1fs", raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS


def _headers() -> dict[str, str]:
    token = _token()
    if not token:
        raise SearchMCPUnavailable("未配置 SEARCH_MCP_TOKEN，搜索 MCP 不可用。")
    return {"Authorization": f"Bearer {token}"}


def _endpoint_error(status_code: int) -> SearchMCPUnavailable:
    if status_code in (401, 403):
        return SearchMCPUnavailable(
            f"搜索 MCP 鉴权失败(HTTP {status_code})，请检查 SEARCH_MCP_TOKEN 是否正确/未过期。"
        )
    return SearchMCPUnavailable(f"搜索 MCP 返回 HTTP {status_code}。")


async def check_search_health() -> dict[str, Any]:
    """探测搜索服务健康（``GET /healthz``，服务端不校验 token）。"""
    base = _url().removesuffix("/mcp").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/healthz")
            if resp.status_code != 200:
                return {"status": "unhealthy", "http_status": resp.status_code}
            return {"status": "ok", **(resp.json() if resp.content else {})}
    except Exception as exc:  # noqa: BLE001 — 健康探测任何失败都归为不可用
        return {"status": "unreachable", "error": f"{type(exc).__name__}: {exc}"}


async def list_search_tools() -> list[SearchToolInfo]:
    """拉取搜索 MCP 暴露的工具列表（名称 / 描述 / inputSchema）。"""
    headers = _headers()
    timeout = _timeout()

    async def _run() -> list[SearchToolInfo]:
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(timeout, read=timeout * 6)
        ) as client:
            async with streamable_http_client(_url(), http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [
                        SearchToolInfo(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema or {},
                        )
                        for t in result.tools
                    ]

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except SearchMCPUnavailable:
        raise
    except asyncio.TimeoutError as exc:
        raise SearchMCPUnavailable(f"搜索 MCP list_tools 超时（>{timeout:.0f}s）。") from exc
    except (httpx.HTTPStatusError, McpError, Exception) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            raise _endpoint_error(exc.response.status_code) from exc
        raise SearchMCPUnavailable(f"搜索 MCP list_tools 失败: {type(exc).__name__}: {exc}") from exc


async def call_search_tool(name: str, arguments: dict[str, Any]) -> str:
    """调用搜索 MCP 工具，返回把 text 内容拼接后的结果文本。

    Raises:
        SearchMCPUnavailable: 会话/网络/鉴权层面的故障，或工具本身报错
            （``isError``）。调用方据此决定回退策略。
    """
    headers = _headers()
    timeout = _timeout()

    async def _run() -> str:
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(timeout, read=timeout * 6)
        ) as client:
            async with streamable_http_client(_url(), http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    chunks = [c.text for c in result.content if getattr(c, "text", None)]
                    payload = "\n".join(chunks)
                    if result.isError:
                        raise SearchMCPUnavailable(f"搜索工具 {name} 执行失败: {payload[:500]}")
                    return payload

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except SearchMCPUnavailable:
        raise
    except asyncio.TimeoutError as exc:
        raise SearchMCPUnavailable(f"搜索 MCP 工具 {name} 超时（>{timeout:.0f}s）。") from exc
    except httpx.HTTPStatusError as exc:
        raise _endpoint_error(exc.response.status_code) from exc
    except Exception as exc:  # noqa: BLE001 — 统一转成可用/不可用两类语义
        raise SearchMCPUnavailable(f"搜索 MCP 调用失败: {type(exc).__name__}: {exc}") from exc


def dump_json(obj: Any) -> str:
    """把结果序列化成给 LLM 看的 JSON（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)
