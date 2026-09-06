"""监控后台循环（设计 §3 ③）：自检 → 评估规则 → 通知 → 同步公告横幅。

- 由 api.main 的 lifespan 以 asyncio task 启动（对齐 _session_cleanup_loop 写法），
  每轮 sleep CTA_ALERT_WATCH_INTERVAL 秒（默认 60s）。
- 自检项（结果写 gauge，供 self_check / disk_usage / db_size / token_budget 规则用）：
  DB 写探针、graph ready、磁盘使用率（data 目录所在盘）、数据库体积、token 日预算消耗。
- 公告横幅联动：user_visible 规则 firing → upsert monitor 公告（每轮次只写一次）；
  resolved / watcher 重启 → 撤下。**启动时全量清空 monitor 横幅**（状态机随进程重置）。
- 整个 tick 包 try/except：监控层任何故障不影响业务，只记日志。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 60


def _interval() -> int:
    try:
        return max(15, int(os.getenv("CTA_ALERT_WATCH_INTERVAL", str(_DEFAULT_INTERVAL))))
    except ValueError:
        return _DEFAULT_INTERVAL


def _alerts_enabled() -> bool:
    val = os.getenv("CTA_ALERTS_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


# ── 自检项 ──────────────────────────────────────────────────

def _db_write_probe() -> bool:
    """真实写探针：建/写/删一张微型探针表。失败=DB 写路径故障。"""
    try:
        from code_tutor_agent.db.database import _with_conn

        def _do(cursor):
            cursor.execute("CREATE TABLE IF NOT EXISTS _monitor_probe (v INTEGER)")
            cursor.execute("INSERT INTO _monitor_probe (v) VALUES (1)")
            cursor.execute("DELETE FROM _monitor_probe WHERE v = 1")
            return True
        return bool(_with_conn(_do))
    except Exception as exc:
        logger.warning("[watcher] db write probe failed: %s", exc)
        return False


def _graph_ready() -> bool:
    try:
        from code_tutor_agent.api.deps import get_graph

        get_graph()
        return True
    except Exception:
        return False


def _disk_usage_pct() -> float | None:
    """data 目录所在盘的使用率（%）。"""
    try:
        from code_tutor_agent.db.database import DB_PATH

        target = os.path.dirname(os.path.abspath(DB_PATH)) or "."
        total, _used, free = shutil.disk_usage(target)
        if total <= 0:
            return None
        return round((total - free) / total * 100, 1)
    except Exception as exc:
        logger.debug("[watcher] disk usage failed (ignored): %s", exc)
        return None


def _db_size_mb() -> float | None:
    """主库 + checkpoint 库 + WAL 的总体积（MB）。"""
    try:
        from code_tutor_agent.config import get_checkpoint_db_path
        from code_tutor_agent.db.database import DB_PATH

        total = 0
        for path in (DB_PATH, get_checkpoint_db_path()):
            for suffix in ("", "-wal", "-shm"):
                p = path + suffix
                if os.path.isfile(p):
                    total += os.path.getsize(p)
        return round(total / (1024 * 1024), 1)
    except Exception as exc:
        logger.debug("[watcher] db size failed (ignored): %s", exc)
        return None


def _token_budget_pct() -> float | None:
    try:
        from code_tutor_agent.config import get_token_daily_budget
        from code_tutor_agent.db.database import get_today_token_cost

        budget = get_token_daily_budget()
        if budget <= 0:
            return None
        return round(get_today_token_cost() / budget * 100, 1)
    except Exception as exc:
        logger.debug("[watcher] token budget failed (ignored): %s", exc)
        return None


def _run_self_checks() -> None:
    """执行全部自检，结果写 gauge（永不抛异常）。"""
    from code_tutor_agent.monitoring.metrics import get_registry

    reg = get_registry()
    db_ok = _db_write_probe()
    graph_ok = _graph_ready()
    reg.set_gauge("db_write_ok", 1.0 if db_ok else 0.0)
    reg.set_gauge("graph_ready", 1.0 if graph_ok else 0.0)

    try:
        from code_tutor_agent.token_usage.sink import get_token_sink

        reg.set_gauge("token_sink_pending", float(get_token_sink().pending()))
    except Exception:
        pass

    disk = _disk_usage_pct()
    if disk is not None:
        reg.set_gauge("disk_usage_pct", disk)
    size = _db_size_mb()
    if size is not None:
        reg.set_gauge("db_size_mb", size)
    budget = _token_budget_pct()
    if budget is not None:
        reg.set_gauge("token_budget_pct", budget)

    reg.set_gauge("self_check_ok", 1.0 if (db_ok and graph_ok) else 0.0)
    if not (db_ok and graph_ok):
        logger.warning("[watcher] self-check FAILED (db_ok=%s graph_ok=%s)", db_ok, graph_ok)


# ── 主循环 ──────────────────────────────────────────────────

class Watcher:
    """规则评估 + 通知 + 公告横幅联动（含每轮 fired 集合的本地记忆）。"""

    def __init__(self) -> None:
        self._announced: dict[str, bool] = {}  # rule_id -> 横幅是否已挂

    def tick(self) -> None:
        from code_tutor_agent.monitoring.metrics import get_registry
        from code_tutor_agent.monitoring.notifier import get_notifier
        from code_tutor_agent.monitoring.rules import RULES, evaluate_all

        _run_self_checks()
        snapshot = get_registry().snapshot()
        fired = evaluate_all(snapshot)
        fired_ids = {item["rule"].rule_id for item in fired}
        notifier = get_notifier()

        # 未触发的 user_visible 规则：撤横幅（每条规则一次 DB 写，之后早退）
        for rule in RULES:
            if rule.user_visible and rule.rule_id not in fired_ids:
                if self._announced.get(rule.rule_id):
                    self._announced[rule.rule_id] = False
                    self._deactivate_banner(rule.rule_id)

        for item in fired:
            rule = item["rule"]
            cooldown = rule.cooldown_min * 60
            notifier.notify_firing(
                rule.rule_id, item["severity"], item["title"], item["detail"],
                cooldown_sec=cooldown,
            )
            # 严重级别动态覆盖（disk 85→warning / 95→critical）
            if rule.user_visible and item["severity"] in ("warning", "critical"):
                if not self._announced.get(rule.rule_id):
                    self._announced[rule.rule_id] = True
                    banner = rule.banner or (item["title"], item["detail"])
                    self._upsert_banner(rule.rule_id, item["severity"], banner[0], banner[1])
                logger.warning(
                    "[alerts] FIRING %s (%s): %s", rule.rule_id, item["severity"], item["detail"]
                )

        # 未触发规则的 resolved 通知（notifier 内部对未 firing 的规则早退）
        for rule in RULES:
            if rule.rule_id not in fired_ids:
                notifier.notify_resolved(rule.rule_id, rule.severity, rule.title, "")

    def _upsert_banner(self, rule_id: str, level: str, title: str, content: str) -> None:
        try:
            from code_tutor_agent.db.database import upsert_monitor_announcement

            upsert_monitor_announcement(rule_id, level, title, content)
        except Exception:
            logger.debug("[watcher] upsert banner failed (ignored)", exc_info=True)

    def _deactivate_banner(self, rule_id: str) -> None:
        try:
            from code_tutor_agent.db.database import deactivate_monitor_announcement

            deactivate_monitor_announcement(rule_id)
        except Exception:
            logger.debug("[watcher] deactivate banner failed (ignored)", exc_info=True)


async def run_watch_loop() -> None:
    """lifespan 启动的监控协程。永不主动退出（随进程取消）。"""
    from code_tutor_agent.monitoring.metrics import get_registry

    # 进程重启 = 告警状态机重置：启动即撤下全部 monitor 横幅，防止孤儿横幅
    try:
        from code_tutor_agent.db.database import deactivate_all_monitor_announcements

        n = deactivate_all_monitor_announcements()
        if n:
            logger.info("[watcher] cleared %d stale monitor banner(s) on startup", n)
    except Exception:
        logger.debug("[watcher] startup banner cleanup failed (ignored)", exc_info=True)

    # 启动后先等一轮，给 graph 初始化/首波请求留时间（对齐 cleanup loop 的做法）
    await asyncio.sleep(30)

    watcher = Watcher()
    interval = _interval()
    logger.info("[watcher] monitoring loop started (interval=%ds)", interval)

    purge_counter = 0
    while True:
        try:
            if _alerts_enabled():
                watcher.tick()
            else:
                get_registry().set_gauge("alerts_enabled", 0.0)
        except Exception as exc:
            logger.exception("[watcher] tick error (ignored): %s", exc)

        # 每 6 小时顺带清一次过期告警历史
        purge_counter += 1
        if purge_counter >= max(1, 360 // max(1, interval // 60)):
            purge_counter = 0
            try:
                from code_tutor_agent.db.database import purge_alerts

                purge_alerts()
            except Exception:
                logger.debug("[watcher] purge_alerts failed (ignored)", exc_info=True)

        await asyncio.sleep(interval)
