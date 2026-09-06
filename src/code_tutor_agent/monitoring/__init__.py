"""监控告警包（docs/monitoring-alerts-design.md）。

模块分层：
- metrics  ：进程内指标注册表（计数器/滑动窗口/streak/gauge），永不抛异常
- rules    ：告警规则定义与评估（rate 型 + streak 型）
- notifier ：冷却状态机 + Brevo 邮件 + alert_history 落库
- watcher  ：自检 + 周期评估 + 公告横幅联动（lifespan 启动）

业务代码只需两件事：
1. 埋点：``get_registry().record("xxx")`` / ``record_streak("xxx", ok=False)``；
2. lifespan 启动 ``monitoring.watcher.run_watch_loop``。
"""
from code_tutor_agent.monitoring.metrics import MetricsRegistry, get_registry

__all__ = ["MetricsRegistry", "get_registry"]
