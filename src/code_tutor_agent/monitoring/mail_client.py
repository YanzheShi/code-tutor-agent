"""告警邮件统一发送客户端：mcp-hub 优先，Brevo 直连兜底（2026-09-07 接入）。

通道优先级：
1. **mcp-hub**（MCP Streamable HTTP，`send_email` 工具）——统一配额/记账由 hub 管理，
   环境变量 ``MCP_HUB_URL`` + ``MCP_HUB_TOKEN`` 配置即启用；
2. **Brevo 直连**（api/email.py）——hub 未配置或调用失败时回退，老链路原样保留。

设计口径（对齐 monitoring 包既有原则）：
- 纯标准库 urllib，零新依赖；MCP 握手三步（initialize → initialized → tools/call）
  每次独立会话，告警低频不心疼开销；
- 任何失败只返回 (False, 原因)，绝不抛异常——调用方（notifier/心跳脚本）只记日志；
- hub 返回 isError=true 时视为失败并**回退直连**（宁可多发一封，不可漏发告警）。

协议细节（mcp-hub README §裸 HTTP 调试）：
- 响应为 SSE ``data: {...}`` 帧，取最后一帧为 JSON-RPC 响应；
- 会话 id 在 initialize 响应头 ``Mcp-Session-Id``，后续请求必须携带。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_DEFAULT_HUB_URL = "http://127.0.0.1:8080/mcp"
_MCP_TIMEOUT = 10


def hub_configured() -> bool:
    """mcp-hub 邮件通道是否可用（URL + token 都在）。"""
    return bool(_hub_url()) and bool(os.getenv("MCP_HUB_TOKEN", "").strip())


def _hub_url() -> str:
    return os.getenv("MCP_HUB_URL", "").strip() or _DEFAULT_HUB_URL


def _rpc(payload: dict, token: str, session_id: str | None = None) -> tuple[dict | None, str | None]:
    """发一次 JSON-RPC POST，解析 SSE 帧取最后一帧。返回 (response, session_id)。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        _hub_url(), json.dumps(payload).encode("utf-8"), headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=_MCP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        data: dict | None = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    pass  # 非法帧跳过，取下一帧
        return data, resp.headers.get("Mcp-Session-Id")


def send_via_hub(to_emails: list[str], subject: str, text: str) -> tuple[bool, str]:
    """经 mcp-hub 的 send_email 工具发信。返回 (ok, detail)。

    detail 为成功时的 messageId 或失败原因（进日志用）。
    """
    token = os.getenv("MCP_HUB_TOKEN", "").strip()
    if not token:
        return False, "MCP_HUB_TOKEN not configured"
    try:
        init, sid = _rpc({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "code-tutor-agent", "version": "1"},
            },
        }, token)
        if init is None or not sid:
            return False, "hub initialize failed (no response / no session id)"
        if "error" in init:
            return False, f"hub initialize error: {init['error'].get('message', '?')[:120]}"

        # 通知帧无响应体也无需检查（服务器按协议静默处理）
        _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, sid)

        call, _ = _rpc({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "send_email",
                "arguments": {
                    "to": [{"email": e} for e in to_emails],
                    "subject": subject,
                    "text": text,
                },
            },
        }, token, sid)
        if call is None:
            return False, "hub tools/call returned no data frame"
        if "error" in call:
            return False, f"hub rpc error: {call['error'].get('message', '?')[:120]}"
        result = call.get("result") or {}
        if result.get("isError"):
            text_content = ""
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    text_content = item["text"]
                    break
            return False, f"hub tool error: {text_content[:120]}"
        # 成功：content[0].text 为 {"messageId": ...}（宽松判定，解析不到也不算失败）
        message_id = ""
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                try:
                    message_id = str(json.loads(item["text"]).get("messageId", ""))
                except Exception:
                    pass
                break
        return True, message_id or "sent via hub"
    except urllib.error.HTTPError as exc:
        return False, f"hub HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]


def send_alert_email(to_emails: list[str], subject: str, body: str) -> tuple[bool, str]:
    """告警邮件统一入口：hub 优先，失败回退 Brevo 直连。

    返回 (ok, detail)，detail 标注实际使用的通道（"hub:..." / "direct:..."）。
    hub 未配置时不尝试（连一次 HTTP 都不浪费）； Brevo 未配置且 hub 失败 → 整体失败。
    """
    if not to_emails:
        return False, "direct: no recipients"

    if hub_configured():
        ok, detail = send_via_hub(to_emails, subject, body)
        if ok:
            return True, f"hub: {detail}"
        logger.warning("[mail] hub 通道失败，回退 Brevo 直连: %s", detail)
    else:
        logger.debug("[mail] mcp-hub 未配置，走 Brevo 直连")

    try:
        from code_tutor_agent.api.email import is_configured, send_email

        if not is_configured():
            return False, "direct: BREVO_API_KEY not configured"
        sent = any(send_email(to, subject, body) for to in to_emails)
        return sent, "direct: sent" if sent else "direct: send failed"
    except Exception as exc:
        return False, f"direct: {type(exc).__name__}: {exc}"[:160]
