"""邮件发送 — 统一经 mcp-hub（2026-09-07 起，Brevo 直连已移除）。

设计口径：
- **唯一通道**：mcp-hub 的 ``send_email`` 工具（``MCP_HUB_URL`` + ``MCP_HUB_TOKEN``，
  统一配额/记账由 hub 管理）；底层实现复用 ``monitoring/mail_client.py`` 的
  裸 HTTP MCP 客户端，本模块只是稳定门面（调用方与测试都 mock 这两个函数）。
- 未配置 hub → ``is_configured()`` 为 False，调用方降级（如忘记密码转 admin 重置）。
- 失败只记日志返回 False，绝不抛出（邮件是增强路径，不能拖垮主流程）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """mcp-hub 邮件通道是否可用（未配置时忘记密码等增强功能降级）。"""
    from code_tutor_agent.monitoring.mail_client import hub_configured

    return hub_configured()


def send_email(to_email: str, subject: str, text: str) -> bool:
    """经 mcp-hub 发一封纯文本邮件。成功 True，任何失败 False（只记日志）。"""
    from code_tutor_agent.monitoring.mail_client import send_via_hub

    ok, detail = send_via_hub([to_email], subject, text)
    if ok:
        logger.info("email sent to %s (subject=%r, via %s)", to_email, subject, detail)
    else:
        logger.error("send_email to %s failed: %s", to_email, detail)
    return ok
