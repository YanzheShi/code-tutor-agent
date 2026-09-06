"""告警通知器：冷却状态机 + 邮件发送 + alert_history 落库（设计 §6）。

防重复轰炸（2026-09-07 用户要求：邮件服务数量有限，相同错误不得连发）：
1. 每规则冷却期（critical 30min / warning 60min / info 24h，规则可覆盖）内静默；
2. 恢复邮件**只在同规则此前真的发出过告警邮件**时才发；
3. 恢复后同样进冷却，防止阈值边缘抖动导致反复横跳刷邮件；
4. 邮件 fire-and-forget（daemon 线程），超时/失败只记日志，绝不阻塞 watcher。
   发送总量控制（每日上限等）后续由 MCP 统一配额服务管理，本模块不做。

降级：BREVO_API_KEY 未配置 → 只落库 + ERROR 日志（对齐 api/email.py 既有口径）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 北京时间（对齐 logging_config 的输出口径）
def _now_str() -> str:
    return datetime.now(timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _recipients() -> list[str]:
    import os

    raw = os.getenv("CTA_ALERT_EMAIL_TO", "").strip()
    if not raw:
        raw = os.getenv("CTA_ADMIN_EMAIL", "").strip()
    return [e.strip() for e in raw.split(",") if e.strip()]


def _cooldown_seconds() -> int:
    import os

    try:
        return max(60, int(os.getenv("CTA_ALERT_COOLDOWN_MIN", "60")) * 60)
    except ValueError:
        return 3600


class Notifier:
    """每规则冷却状态机。线程安全（watcher 单协程调用，防御性加锁）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # rule_id -> {"fired_at": monotonic, "notified": bool, "firing": bool, "cooldown": sec}
        self._states: dict[str, dict] = {}

    # ── 通知入口 ────────────────────────────────────────────
    def notify_firing(self, rule_id: str, severity: str, title: str, detail: str,
                      cooldown_sec: int | None = None) -> None:
        """规则触发。冷却期内静默（但保持 firing 状态）；超冷却再次告警。

        cooldown_sec: 本规则专属冷却（秒），None 用全局默认；恢复邮件沿用同值。
        """
        now = time.monotonic()
        cd = int(cooldown_sec) if cooldown_sec and cooldown_sec > 0 else _cooldown_seconds()
        with self._lock:
            st = self._states.setdefault(
                rule_id, {"fired_at": 0.0, "notified": False, "firing": False, "cooldown": cd}
            )
            st["cooldown"] = cd
            was_firing = st["firing"]
            in_cooldown = (now - st["fired_at"]) < st["cooldown"]
            st["firing"] = True

        # 已在 firing 中且处于冷却内 → 完全静默（不发邮件、不重复落库）
        if was_firing and in_cooldown:
            return

        # 冷却到期仍在 firing → 再发一轮；首次触发 → 立即发
        self._emit(rule_id, "firing", severity, title, detail, cooldown_sec=cd)

    def notify_resolved(self, rule_id: str, severity: str, title: str, detail: str) -> None:
        """规则解除。只有此前真发过告警邮件才发恢复邮件。"""
        with self._lock:
            st = self._states.get(rule_id)
            if st is None or not st["firing"]:
                return  # 从未触发过，无所谓恢复
            st["firing"] = False
            st["fired_at"] = time.monotonic()  # 恢复也进冷却，防抖动反复横跳
            had_notified = st["notified"]
            st["notified"] = False
            cd = st.get("cooldown") or _cooldown_seconds()
        if not had_notified:
            return
        self._emit(rule_id, "resolved", severity, title, detail, cooldown_sec=cd)

    # ── 实际发送 + 落库 ─────────────────────────────────────
    def _emit(self, rule_id: str, status: str, severity: str, title: str, detail: str,
              cooldown_sec: int | None = None) -> None:
        """落库 → 异步发信。任何异常不外抛。"""
        try:
            cd = int(cooldown_sec) if cooldown_sec and cooldown_sec > 0 else _cooldown_seconds()
            notified = self._send_email_async(rule_id, status, severity, title, detail, cd)
            self._save(rule_id, severity, status, title, detail, notified=notified)

            with self._lock:
                if status == "firing":
                    st = self._states.setdefault(
                        rule_id,
                        {"fired_at": 0.0, "notified": False, "firing": True, "cooldown": cd},
                    )
                    st["fired_at"] = time.monotonic()
                    st["notified"] = notified
        except Exception:
            logger.warning("[alerts] emit failed (ignored): %s", rule_id, exc_info=True)

    def _save(self, rule_id: str, severity: str, status: str, title: str,
              detail: str, notified: bool) -> None:
        try:
            from code_tutor_agent.db.database import save_alert

            save_alert(rule_id, severity, status, title, detail, notified=notified)
        except Exception:
            logger.debug("[alerts] save_alert failed (ignored)", exc_info=True)

    def _send_email_async(self, rule_id: str, status: str, severity: str,
                          title: str, detail: str, cooldown_sec: int) -> bool:
        """daemon 线程发信；返回 False 表示未配置（同步判定）。"""
        from code_tutor_agent.api.email import is_configured

        if not is_configured():
            logger.error(
                "[alerts] BREVO_API_KEY 未配置，告警只落库不发信：%s %s", rule_id, title
            )
            return False

        recipients = _recipients()
        if not recipients:
            logger.error("[alerts] 无收件人（CTA_ALERT_EMAIL_TO/CTA_ADMIN_EMAIL 均空），只落库")
            return False

        tag = "已恢复" if status == "resolved" else "告警"
        subject = f"[CodeTutor {tag}][{severity.upper()}] {rule_id}"
        body = (
            f"[CodeTutor {tag}][{severity.upper()}] {rule_id}\n"
            f"标题: {title}\n"
            f"时间: {_now_str()} (UTC+8)\n"
            f"详情: {detail}\n"
            f"---\n"
            f"本邮件由 code-tutor-agent 监控系统发送；"
            f"同一规则 {max(1, int(cooldown_sec // 60))} 分钟内不重复告警。"
        )

        def _send() -> None:
            try:
                from code_tutor_agent.api.email import send_email

                ok = False
                for to in recipients:
                    if send_email(to, subject, body):
                        ok = True
                if not ok:
                    logger.warning("[alerts] 告警邮件全部发送失败: %s", rule_id)
            except Exception:
                logger.warning("[alerts] 告警邮件线程异常 (ignored): %s", rule_id, exc_info=True)

        threading.Thread(target=_send, name=f"alert-mail-{rule_id}", daemon=True).start()
        return True


# ── 进程级单例 ──────────────────────────────────────────────
_NOTIFIER: Notifier | None = None
_NOTIFIER_LOCK = threading.Lock()


def get_notifier() -> Notifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        with _NOTIFIER_LOCK:
            if _NOTIFIER is None:
                _NOTIFIER = Notifier()
    return _NOTIFIER
