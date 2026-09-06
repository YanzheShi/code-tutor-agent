"""邮件发送 — Brevo（原 Sendinblue）HTTP API，可选增强。

设计口径（2026-09-06 注册防滥用改造）：
- 未配置 BREVO_API_KEY → is_configured() 为 False，调用方降级（如忘记密码转 admin 重置）。
- 纯标准库 urllib，零新依赖；同步调用（用于登录/注册等低频路径，不做异步封装）。
- 发件人必须已在 Brevo 控制台验证：BREVO_SENDER 环境变量，默认用管理员邮箱。
- 失败只记日志返回 False，绝不抛出（邮件是增强路径，不能拖垮主流程）。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def is_configured() -> bool:
    """是否已配置 Brevo（未配置时忘记密码等增强功能降级）。"""
    return bool(os.getenv("BREVO_API_KEY", "").strip())


def _sender() -> dict:
    email = os.getenv("BREVO_SENDER", "").strip() or os.getenv(
        "CTA_ADMIN_EMAIL", "534629255@qq.com"
    ).strip()
    return {"email": email, "name": "Code Tutor"}


def send_email(to_email: str, subject: str, text: str) -> bool:
    """通过 Brevo 发一封纯文本邮件。成功 True，任何失败 False（只记日志）。"""
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    if not api_key:
        logger.warning("send_email skipped: BREVO_API_KEY not configured")
        return False
    payload = json.dumps({
        "sender": _sender(),
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        _BREVO_URL,
        data=payload,
        method="POST",
        headers={
            "api-key": api_key,
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                logger.info("email sent to %s (subject=%r)", to_email, subject)
            return ok
    except Exception as exc:  # 网络/4xx/5xx 一律降级
        logger.error("send_email to %s failed: %s", to_email, exc)
        return False
