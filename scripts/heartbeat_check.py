"""进程外心跳探活（docs/monitoring-alerts-design.md §7）。

解决「进程挂了进程内监控是瞎的」盲区：本脚本**独立于后端进程**运行，
由系统计划任务（Windows 任务计划 / cron）每 2 分钟调度一次：

1. GET  {CTA_HEALTHCHECK_URL}/health（默认 http://127.0.0.1:8000，超时 10s）
2. 状态写 data/heartbeat.state（连续失败计数 + 上次结果）
3. 连续 ≥2 次失败 → 直接发 critical 告警邮件（不依赖后端进程活着）
4. 此前失败、本次成功 → 发恢复邮件
5. 单次失败不发（网络抖动静默），避免误报轰炸

退出码：0 健康 / 1 探活失败 / 2 配置错误。**不 sleep 循环**——调度交给计划任务，
每次运行做完就走，进程常驻反而引入第二个需要被监控的东西。

用法（手动测试）：
    python scripts/heartbeat_check.py
计划任务命令示例（Windows）：
    "C:\\...\\python.exe" D:\\Code\\PycharmProjects\\code-tutor-agent\\scripts\\heartbeat_check.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# .env 不自动加载（计划任务环境下读不到也算降级路径）——容忍直接环境变量注入
_FAIL_THRESHOLD = 2
_TIMEOUT = 10


def _utc8_now() -> str:
    return datetime.now(timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _state_path() -> Path:
    env = os.getenv("CTA_HEARTBEAT_STATE")
    if env:
        return Path(env)
    return REPO_ROOT / "data" / "heartbeat.state"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"consecutive_failures": 0, "alerted": False}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[heartbeat] WARN save state failed: {exc}", file=sys.stderr)


def _health_url() -> str:
    base = os.getenv("CTA_HEALTHCHECK_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/health"


def _probe() -> tuple[bool, str]:
    """返回 (ok, detail)。任何非 2xx / 超时 / 连接拒绝都算失败。"""
    try:
        req = urllib.request.Request(_health_url(), method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                return True, body[:200]
            return False, f"HTTP {resp.status}: {body[:200]}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _send_alert(subject: str, body: str) -> bool:
    """直接走 Brevo 发信（复用 api/email.py，不依赖后端进程）。"""
    try:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from code_tutor_agent.api.email import is_configured, send_email

        if not is_configured():
            print("[heartbeat] ERROR BREVO_API_KEY not configured; cannot alert", file=sys.stderr)
            return False
        raw = os.getenv("CTA_ALERT_EMAIL_TO", "") or os.getenv("CTA_ADMIN_EMAIL", "")
        recipients = [e.strip() for e in raw.split(",") if e.strip()]
        if not recipients:
            print("[heartbeat] ERROR no recipients; cannot alert", file=sys.stderr)
            return False
        ok = any(send_email(to, subject, body) for to in recipients)
        return ok
    except Exception as exc:
        print(f"[heartbeat] ERROR send failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    _load_dotenv()
    ok, detail = _probe()
    state = _load_state()
    prev_failures = int(state.get("consecutive_failures", 0))
    prev_alerted = bool(state.get("alerted", False))

    if ok:
        state["consecutive_failures"] = 0
        state["last_ok_at"] = _utc8_now()
        alerted = False
        if prev_alerted:
            # 恢复通知（只在真发过告警时发，省配额）
            alerted = not _send_alert(
                "[CodeTutor 已恢复][CRITICAL] heartbeat",
                f"[CodeTutor 已恢复][CRITICAL] heartbeat\n"
                f"时间: {_utc8_now()} (UTC+8)\n"
                f"详情: /health 恢复 2xx（此前连续失败 {prev_failures} 次）\n",
            )
        state["alerted"] = prev_alerted and alerted  # 恢复成功即清标记
        _save_state(state)
        print(f"[heartbeat] OK {_health_url()} {detail[:120]}")
        return 0

    failures = prev_failures + 1
    state["consecutive_failures"] = failures
    state["last_fail_at"] = _utc8_now()
    state["last_fail_detail"] = detail[:300]
    print(f"[heartbeat] FAIL ({failures}/{_FAIL_THRESHOLD}): {detail[:200]}", file=sys.stderr)

    if failures >= _FAIL_THRESHOLD and not prev_alerted:
        sent = _send_alert(
            "[CodeTutor 告警][CRITICAL] heartbeat",
            f"[CodeTutor 告警][CRITICAL] heartbeat\n"
            f"时间: {_utc8_now()} (UTC+8)\n"
            f"详情: /health 连续 {failures} 次探活失败（阈值 {_FAIL_THRESHOLD}）\n"
            f"错误: {detail[:300]}\n"
            f"目标: {_health_url()}\n"
            f"---\n"
            f"进程可能已挂掉或假死，请尽快检查后端服务。",
        )
        state["alerted"] = sent  # 发送成功才标记，失败下轮重试
    _save_state(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
