"""告警规则定义与评估（设计 §5 + §14）。

规则分两类口径：
- rate 型：滑动窗口内比例/次数达到阈值（依赖流量基数）；
- streak 型：某环节**连续**失败 N 次（成功清零，低流量也能触发）——凡「某环节
  连续挂」的场景一律用 streak，对齐 2026-09-07 评审结论。

每条规则带：
- severity      ：info / warning / critical
- cooldown_min  ：该规则专属冷却（邮件配额保护，配额有限 → 默认就拉长）
- user_visible  ：触发时是否同步主页公告横幅（仅「用户能感知的降级」）
- evaluate()    ：输入 metrics snapshot，返回 (triggered, title, detail)

阈值全部支持环境变量覆盖；评估函数本身绝不抛异常（内部全 try/except）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# 规则专属冷却（分钟）：critical 30、warning 60、低频资源类 360/1440。
# 同规则冷却内绝不重发（邮件服务数量有限，防相同错误重复轰炸）；
# 发送总量控制后续由 MCP 统一配额服务管理。
_CD_CRITICAL = _env_int("CTA_ALERT_COOLDOWN_CRITICAL_MIN", 30)
_CD_WARNING = _env_int("CTA_ALERT_COOLDOWN_WARNING_MIN", 60)
_CD_INFO = _env_int("CTA_ALERT_COOLDOWN_INFO_MIN", 1440)


@dataclass
class Rule:
    rule_id: str
    severity: str
    title: str
    user_visible: bool = False
    cooldown_min: int = _CD_WARNING
    # 触发时挂主页横幅的文案（title, content）；user_visible=True 时必填
    banner: tuple[str, str] | None = None
    # 评估函数：snapshot -> (triggered, detail)
    evaluate: Callable[[dict], tuple[bool, str]] = field(repr=False, default=None)  # type: ignore[assignment]


# ── 评估函数 ────────────────────────────────────────────────

def _win(snapshot: dict, name: str, horizon: str = "5m") -> int:
    return int(snapshot.get("windows", {}).get(name, {}).get(horizon, 0))


def _streak(snapshot: dict, name: str) -> int:
    return int(snapshot.get("streaks", {}).get(name, 0))


def _gauge(snapshot: dict, name: str) -> float | None:
    val = snapshot.get("gauges", {}).get(name)
    return None if val is None else float(val)


def _eval_http_5xx_rate(snapshot: dict) -> tuple[bool, str]:
    total = _win(snapshot, "http_total")
    n5xx = _win(snapshot, "http_5xx")
    if total < 20 or n5xx == 0:
        return False, ""
    ratio = n5xx / total
    if ratio > 0.10:
        return True, f"近 5 分钟 5xx 率 {ratio:.1%}（{n5xx}/{total} 请求）"
    return False, ""


def _eval_llm_fail_streak(snapshot: dict) -> tuple[bool, str]:
    n = _streak(snapshot, "llm_fail")
    if n >= 3:
        return True, f"LLM 调用连续失败 {n} 次（failover 已耗尽或未配置）"
    return False, ""


def _eval_llm_failover_rate(snapshot: dict) -> tuple[bool, str]:
    n = _win(snapshot, "llm_failover")
    if n >= 5:
        return True, f"近 5 分钟 LLM failover 触发 {n} 次（主模型/网关不稳定）"
    return False, ""


def _eval_db_locked(snapshot: dict) -> tuple[bool, str]:
    n = _win(snapshot, "db_locked")
    if n >= 3:
        return True, f"近 5 分钟 database is locked {n} 次（写锁争用，看慢事务日志抓持锁线程）"
    return False, ""


def _eval_disk_usage(snapshot: dict) -> tuple[bool, str]:
    pct = _gauge(snapshot, "disk_usage_pct")
    if pct is None:
        return False, ""
    critical_th = _env_float("CTA_ALERT_DISK_CRITICAL", "95")
    warn_th = _env_float("CTA_ALERT_DISK_THRESHOLD", "85")
    if pct >= critical_th:
        return True, f"磁盘使用率 {pct:.0f}%（≥{critical_th:.0f}%，data 卷将写满，SQLite/checkpoint 会损坏）"
    if pct >= warn_th:
        return True, f"磁盘使用率 {pct:.0f}%（≥{warn_th:.0f}%）"
    return False, ""


def _eval_db_size(snapshot: dict) -> tuple[bool, str]:
    mb = _gauge(snapshot, "db_size_mb")
    if mb is None:
        return False, ""
    th = _env_float("CTA_ALERT_DB_SIZE_MB", "1024")
    if mb >= th:
        return True, f"数据库体积 {mb:.0f} MB（≥{th:.0f} MB，考虑跑 scripts/cleanup_traces.py）"
    return False, ""


def _eval_token_budget(snapshot: dict) -> tuple[bool, str]:
    pct = _gauge(snapshot, "token_budget_pct")
    if pct is None:
        return False, ""
    if pct >= 80:
        return True, f"今日 token 成本已达日预算 {pct:.0f}%"
    return False, ""


def _eval_judge_fail_rate(snapshot: dict) -> tuple[bool, str]:
    total = _win(snapshot, "judge_total")
    nerr = _win(snapshot, "judge_error")
    if total < 10 or nerr == 0:
        return False, ""
    ratio = nerr / total
    if ratio > 0.30:
        return True, f"近 5 分钟判题错误率 {ratio:.1%}（{nerr}/{total}）"
    return False, ""


def _eval_judge_fail_streak(snapshot: dict) -> tuple[bool, str]:
    n = _streak(snapshot, "judge_fail")
    if n >= 3:
        return True, f"判题连续失败 {n} 次（judge 后端/沙箱疑似不可用，提交将持续报错）"
    return False, ""


def _eval_graph_fail_streak(snapshot: dict) -> tuple[bool, str]:
    n = _streak(snapshot, "graph_fail")
    if n >= 2:
        return True, f"LangGraph 调用连续异常 {n} 次（辅导主流程疑似中断，看异常栈定位节点）"
    return False, ""


def _eval_client_error_rate(snapshot: dict) -> tuple[bool, str]:
    n = _win(snapshot, "client_error", "10m")
    if n >= 10:
        return True, f"近 10 分钟前端错误上报 {n} 条（页面异常/加载失败）"
    return False, ""


def _eval_cleanup_loop(snapshot: dict) -> tuple[bool, str]:
    n = _streak(snapshot, "cleanup_loop_fail")
    if n >= 3:
        return True, f"后台清理循环连续异常 {n} 轮（会话 TTL 清理停摆）"
    return False, ""


def _eval_self_check(snapshot: dict) -> tuple[bool, str]:
    ok = _gauge(snapshot, "self_check_ok")
    if ok is not None and ok < 1:
        parts = []
        if _gauge(snapshot, "db_write_ok") is not None and _gauge(snapshot, "db_write_ok") < 1:
            parts.append("DB 写探针失败")
        if _gauge(snapshot, "graph_ready") is not None and _gauge(snapshot, "graph_ready") < 1:
            parts.append("graph 未就绪")
        if _gauge(snapshot, "token_sink_pending") is not None \
                and _gauge(snapshot, "token_sink_pending") > 5000:
            parts.append("token sink 积压")
        return True, "；".join(parts) or "自检失败"
    return False, ""


def disk_rule_severity(snapshot: dict) -> str:
    pct = _gauge(snapshot, "disk_usage_pct")
    return "critical" if (pct is not None and pct >= _env_float("CTA_ALERT_DISK_CRITICAL", "95")) \
        else "warning"


# ── 规则表 ──────────────────────────────────────────────────
# 冷却宁长勿短（critical 30min / warning 60min / info 24h）；
# streak 阈值保守（judge ≥3、graph ≥2、llm ≥3）。

RULES: list[Rule] = [
    Rule("http_5xx_rate", "critical", "HTTP 5xx 错误率过高",
         cooldown_min=_CD_CRITICAL, evaluate=_eval_http_5xx_rate),
    Rule("llm_fail_streak", "critical", "LLM 调用连续失败",
         cooldown_min=_CD_CRITICAL, evaluate=_eval_llm_fail_streak),
    Rule("llm_failover_rate", "warning", "LLM failover 频繁触发",
         cooldown_min=_CD_WARNING, evaluate=_eval_llm_failover_rate),
    Rule("db_locked_storm", "warning", "SQLite 写锁争用风暴",
         cooldown_min=_CD_WARNING, user_visible=True,
         banner=("系统繁忙", "数据库写入压力较大，操作响应可能变慢，请稍候重试。"),
         evaluate=_eval_db_locked),
    Rule("disk_usage", "warning", "磁盘使用率告警",
         cooldown_min=_env_int("CTA_ALERT_COOLDOWN_DISK_MIN", 360), evaluate=_eval_disk_usage),
    Rule("db_size", "info", "数据库体积过大",
         cooldown_min=_CD_INFO, evaluate=_eval_db_size),
    Rule("token_budget", "warning", "Token 日预算消耗告警",
         cooldown_min=_CD_INFO, evaluate=_eval_token_budget),
    Rule("judge_fail_rate", "warning", "判题错误率过高",
         cooldown_min=_CD_WARNING, evaluate=_eval_judge_fail_rate),
    Rule("judge_fail_streak", "critical", "判题连续失败",
         cooldown_min=_CD_CRITICAL, user_visible=True,
         banner=("判题服务不稳定", "判题服务暂时不稳定，代码提交可能延迟或失败，请稍后重试。"),
         evaluate=_eval_judge_fail_streak),
    Rule("graph_fail_streak", "critical", "LangGraph 调用连续异常",
         cooldown_min=_CD_CRITICAL, user_visible=True,
         banner=("辅导服务中断", "辅导服务暂时中断，请刷新页面重试；进行中的会话已保存。"),
         evaluate=_eval_graph_fail_streak),
    Rule("client_error_rate", "warning", "前端错误激增",
         cooldown_min=_CD_WARNING, evaluate=_eval_client_error_rate),
    Rule("cleanup_loop_error", "warning", "后台清理循环异常",
         cooldown_min=_env_int("CTA_ALERT_COOLDOWN_CLEANUP_MIN", 360), evaluate=_eval_cleanup_loop),
    Rule("self_check", "critical", "系统自检失败",
         cooldown_min=_CD_CRITICAL, evaluate=_eval_self_check),
]

# disk_usage 的严重级别动态判定（85 warning / 95 critical）——评估后覆盖
_DYNAMIC_SEVERITY = {"disk_usage": disk_rule_severity}


def evaluate_all(snapshot: dict) -> list[dict]:
    """评估全部规则，返回触发列表。

    每项：{"rule": Rule, "severity": str, "title": str, "detail": str}
    评估异常按「未触发」处理并记日志（监控层故障不影响业务）。
    """
    fired: list[dict] = []
    for rule in RULES:
        try:
            triggered, detail = rule.evaluate(snapshot or {})
        except Exception:
            logger.debug("[rules] %s evaluate failed (ignored)", rule.rule_id, exc_info=True)
            continue
        if not triggered:
            continue
        severity = rule.severity
        dyn = _DYNAMIC_SEVERITY.get(rule.rule_id)
        if dyn is not None:
            try:
                severity = dyn(snapshot)
            except Exception:
                pass
        fired.append({
            "rule": rule,
            "severity": severity,
            "title": rule.title,
            "detail": detail,
        })
    return fired


def get_rule(rule_id: str) -> Rule | None:
    for rule in RULES:
        if rule.rule_id == rule_id:
            return rule
    return None
