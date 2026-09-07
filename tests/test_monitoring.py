"""监控告警系统单测（docs/monitoring-alerts-design.md §13）。

覆盖：
- Window：窗口滑出、count 口径
- MetricsRegistry：record/streak/gauge/snapshot、异常不外抛
- Notifier：冷却防重发、恢复邮件只在真发过告警时发、未配置降级（mock send_email）
- Rules：各规则阈值边界（5xx 率、streak 阈值、磁盘动态级别）
- DB：alert_history / announcements 表读写（走 CTA_DB_PATH 临时库，不用真实库）

运行口径：本文件独立跑（python -m pytest tests/test_monitoring.py -v），
避免与运行中共库（工作记忆铁律）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

import pytest

# 监控包不依赖 DB 路径，但 notifier 落库会 import database —— 统一指向临时库
_TMPDIR = tempfile.mkdtemp(prefix="cta_monitoring_")
os.environ.setdefault("CTA_DB_PATH", str(Path(_TMPDIR) / "test.db"))
os.environ.setdefault("CTA_ALERTS_ENABLED", "1")
# 防真发邮件（重要）：本机 .env 里的 MCP_HUB_TOKEN（邮件与搜索统一通道） 会被 config.load_dotenv
# 读进测试进程（load_dotenv 不覆盖已有变量，这里先强制置空即可隔离两个通道）。
# hub/直连通道行为由 mock 覆盖，绝不发真邮件。
os.environ["MCP_HUB_TOKEN"] = ""


from code_tutor_agent.monitoring.metrics import MetricsRegistry, Window, get_registry, record_graph_call
from code_tutor_agent.monitoring.notifier import Notifier
from code_tutor_agent.monitoring.rules import RULES, evaluate_all, get_rule


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    """临时库建表（alert_history / announcements 等），模块内一次性。"""
    from code_tutor_agent.db.database import init_db

    init_db()
    yield


# ── Window ──────────────────────────────────────────────────

class TestWindow:
    def test_count_basic(self):
        w = Window(window_seconds=600)
        w.add(1, ts=100.0)
        w.add(1, ts=200.0)
        w.add(1, ts=300.0)
        assert w.count(150, ts=300.0) == 2   # 只数 150s 窗口内的 2 条
        assert w.count(300, ts=300.0) == 3

    def test_eviction(self):
        w = Window(window_seconds=100)
        w.add(1, ts=1000.0)
        w.add(1, ts=2000.0)
        assert len(w) == 1  # 老数据被滑出

    def test_values_sum(self):
        w = Window(window_seconds=600)
        w.add(2.5, ts=100.0)
        w.add(0.5, ts=150.0)
        assert w.count(600, ts=200.0) == 3


# ── MetricsRegistry ─────────────────────────────────────────

class TestRegistry:
    def test_record_counts_and_windows(self):
        reg = MetricsRegistry()
        reg.record("http_total")
        reg.record("http_total")
        reg.record("http_5xx")
        snap = reg.snapshot()
        assert snap["counters"]["http_total"] == 2
        assert snap["windows"]["http_total"]["5m"] == 2
        assert snap["windows"]["http_5xx"]["5m"] == 1

    def test_streak_reset_on_success(self):
        reg = MetricsRegistry()
        assert reg.record_streak("judge_fail", ok=False) == 1
        assert reg.record_streak("judge_fail", ok=False) == 2
        assert reg.record_streak("judge_fail", ok=False) == 3
        assert reg.record_streak("judge_fail", ok=True) == 0
        assert reg.record_streak("judge_fail", ok=False) == 1

    def test_gauge_and_snapshot(self):
        reg = MetricsRegistry()
        reg.set_gauge("disk_usage_pct", 87.5)
        snap = reg.snapshot()
        assert snap["gauges"]["disk_usage_pct"] == 87.5

    def test_record_never_raises(self, monkeypatch):
        reg = MetricsRegistry()
        # 打爆内部状态也不外抛
        monkeypatch.setattr(reg, "record", reg.record)  # no-op patch 保持引用
        reg.record("anything", 1.0)
        reg.record_streak("x", ok=False)
        reg.set_gauge("y", 1)
        # 非 float value 触发内部异常路径也应被吞掉
        reg.record("bad", value="not-a-number")  # type: ignore[arg-type]
        reg.record_streak("bad2", ok=None)  # type: ignore[arg-type]


def test_singleton():
    assert get_registry() is get_registry()


def test_record_graph_call_ok_and_fail():
    """record_graph_call 写的是进程单例，直接对单例断言。"""
    reg = get_registry()
    ok_before = reg.snapshot()["counters"].get("graph_ok", 0)
    fail_streak_before = reg.streak("graph_fail")
    record_graph_call("test_entry", True)
    assert reg.snapshot()["counters"]["graph_ok"] == ok_before + 1
    assert reg.streak("graph_fail") == 0
    record_graph_call("test_entry", False)
    assert reg.snapshot()["counters"].get("graph_fail", 0) >= 1
    assert reg.streak("graph_fail") == fail_streak_before + 1


# ── Notifier ────────────────────────────────────────────────

@pytest.fixture()
def notifier(monkeypatch):
    """未配置 Brevo 的 Notifier（落库路径真实走临时库）。"""
    monkeypatch.setenv("CTA_ALERT_COOLDOWN_MIN", "60")
    n = Notifier()
    yield n


class TestNotifier:
    def test_unconfigured_email_only_saves(self, notifier):
        # mcp-hub 未配置 → notified=False，但告警已落库
        os.environ.pop("MCP_HUB_TOKEN", None)
        notifier.notify_firing("rule_x", "critical", "标题", "详情")
        from code_tutor_agent.db.database import get_recent_alerts

        alerts = get_recent_alerts()
        assert any(a["rule_id"] == "rule_x" and a["status"] == "firing" for a in alerts)

    def test_cooldown_silences_repeat(self, notifier, monkeypatch):
        os.environ.pop("MCP_HUB_TOKEN", None)
        from code_tutor_agent.db.database import get_recent_alerts, save_alert

        # 直接造状态：firing 刚发过（notified=True）
        notifier._states["rule_y"] = {
            "fired_at": time.monotonic(), "notified": True, "firing": True, "cooldown": 3600,
        }
        before = len([a for a in get_recent_alerts() if a["rule_id"] == "rule_y"])
        notifier.notify_firing("rule_y", "critical", "标题", "详情")
        after = len([a for a in get_recent_alerts() if a["rule_id"] == "rule_y"])
        assert after == before  # 冷却期内零落库零发信

    def test_cooldown_expiry_refires(self, notifier):
        os.environ.pop("MCP_HUB_TOKEN", None)
        notifier._states["rule_z"] = {
            "fired_at": time.monotonic() - 7200, "notified": True, "firing": True, "cooldown": 3600,
        }
        notifier.notify_firing("rule_z", "critical", "标题", "详情")
        from code_tutor_agent.db.database import get_recent_alerts

        assert any(a["rule_id"] == "rule_z" and a["status"] == "firing" for a in get_recent_alerts())

    def test_resolved_without_notify_is_silent(self, notifier):
        """从未发过告警邮件（notified=False）→ 恢复不发邮件不落库。"""
        from code_tutor_agent.db.database import get_recent_alerts

        notifier._states["rule_w"] = {
            "fired_at": time.monotonic(), "notified": False, "firing": True, "cooldown": 3600,
        }
        notifier.notify_resolved("rule_w", "critical", "标题", "")
        assert not any(a["rule_id"] == "rule_w" for a in get_recent_alerts())

    def test_resolved_with_notify_emits(self, notifier, monkeypatch):
        """真发过告警 → 恢复邮件走异步线程，落库 resolved。"""
        sent = []
        monkeypatch.setattr(
            "code_tutor_agent.api.email.is_configured", lambda: True, raising=True,
        )
        monkeypatch.setenv("CTA_ALERT_EMAIL_TO", "ops@example.com")

        import code_tutor_agent.monitoring.notifier as notifier_mod

        def _fake_send_email_async(self, rule_id, status, severity, title, detail, cd):
            sent.append((rule_id, status))
            return True

        monkeypatch.setattr(notifier_mod.Notifier, "_send_email_async", _fake_send_email_async)
        notifier._states["rule_v"] = {
            "fired_at": time.monotonic(), "notified": True, "firing": True, "cooldown": 3600,
        }
        notifier.notify_resolved("rule_v", "critical", "标题", "")
        assert ("rule_v", "resolved") in sent

        from code_tutor_agent.db.database import get_recent_alerts

        assert any(a["rule_id"] == "rule_v" and a["status"] == "resolved" for a in get_recent_alerts())

    def test_resolve_then_refire_alerts_again(self, notifier):
        """恢复后再次触发：必须能重新告警（防横跳的冷却只作用于恢复侧）。"""
        os.environ.pop("MCP_HUB_TOKEN", None)
        notifier._states["rule_u"] = {
            "fired_at": time.monotonic(), "notified": False, "firing": False, "cooldown": 3600,
        }
        notifier.notify_firing("rule_u", "warning", "标题", "详情")
        from code_tutor_agent.db.database import get_recent_alerts

        assert any(a["rule_id"] == "rule_u" and a["status"] == "firing" for a in get_recent_alerts())


# ── Rules ───────────────────────────────────────────────────

def _snap(**overrides) -> dict:
    base = {"counters": {}, "streaks": {}, "gauges": {}, "windows": {}}
    base.update(overrides)
    return base


class TestRules:
    def test_http_5xx_rate_below_min_samples(self):
        snap = _snap(windows={"http_total": {"5m": 10}, "http_5xx": {"5m": 5}})
        assert evaluate_all(snap) == []  # 样本 <20 不触发（避免小流量误报）

    def test_http_5xx_rate_triggers(self):
        snap = _snap(windows={"http_total": {"5m": 100}, "http_5xx": {"5m": 20}})
        fired = {item["rule"].rule_id for item in evaluate_all(snap)}
        assert "http_5xx_rate" in fired

    def test_judge_streak_threshold(self):
        snap = _snap(streaks={"judge_fail": 2})
        assert "judge_fail_streak" not in {i["rule"].rule_id for i in evaluate_all(snap)}
        snap = _snap(streaks={"judge_fail": 3})
        fired = [i for i in evaluate_all(snap) if i["rule"].rule_id == "judge_fail_streak"]
        assert fired and fired[0]["severity"] == "critical"
        assert fired[0]["rule"].user_visible and fired[0]["rule"].banner

    def test_graph_streak_threshold(self):
        snap = _snap(streaks={"graph_fail": 2})
        fired = [i for i in evaluate_all(snap) if i["rule"].rule_id == "graph_fail_streak"]
        assert fired and fired[0]["severity"] == "critical"

    def test_disk_dynamic_severity(self):
        snap = _snap(gauges={"disk_usage_pct": 87})
        fired = [i for i in evaluate_all(snap) if i["rule"].rule_id == "disk_usage"]
        assert fired and fired[0]["severity"] == "warning"
        snap = _snap(gauges={"disk_usage_pct": 96})
        fired = [i for i in evaluate_all(snap) if i["rule"].rule_id == "disk_usage"]
        assert fired and fired[0]["severity"] == "critical"

    def test_db_size_info(self):
        snap = _snap(gauges={"db_size_mb": 2048})
        fired = [i for i in evaluate_all(snap) if i["rule"].rule_id == "db_size"]
        assert fired and fired[0]["severity"] == "info"

    def test_client_error_rate(self):
        snap = _snap(windows={"client_error": {"10m": 10}})
        assert "client_error_rate" in {i["rule"].rule_id for i in evaluate_all(snap)}

    def test_empty_snapshot_no_fire(self):
        assert evaluate_all({}) == []
        assert evaluate_all(_snap()) == []

    def test_rule_ids_unique(self):
        ids = [r.rule_id for r in RULES]
        assert len(ids) == len(set(ids))

    def test_get_rule(self):
        assert get_rule("judge_fail_streak") is not None
        assert get_rule("nonexistent") is None

    def test_user_visible_rules_have_banner(self):
        for rule in RULES:
            if rule.user_visible:
                assert rule.banner and rule.banner[0] and rule.banner[1], rule.rule_id


# ── mail_client：hub 优先 + 直连兜底 ────────────────────────

class TestMailClient:
    """通道选择逻辑。_rpc 全 mock，任何用例都不发真邮件。"""

    def _setup_env(self, monkeypatch, hub_token="tok"):
        monkeypatch.setenv("MCP_HUB_URL", "http://127.0.0.1:9999/mcp")
        if hub_token:
            monkeypatch.setenv("MCP_HUB_TOKEN", hub_token)
        else:
            monkeypatch.delenv("MCP_HUB_TOKEN", raising=False)

    def test_hub_success_no_fallback(self, monkeypatch):
        import code_tutor_agent.monitoring.mail_client as mc

        self._setup_env(monkeypatch)
        hub_calls = []
        direct_calls = []

        def fake_rpc(payload, token, session_id=None):
            hub_calls.append(payload.get("method"))
            if payload.get("method") == "initialize":
                return {"jsonrpc": "2.0", "id": 1, "result": {}}, "sid-1"
            return {
                "jsonrpc": "2.0", "id": 2,
                "result": {"content": [{"type": "text", "text": '{"messageId":"m-1"}'}]},
            }, "sid-1"

        monkeypatch.setattr(mc, "_rpc", fake_rpc)
        monkeypatch.setattr("code_tutor_agent.api.email.is_configured", lambda: True)
        monkeypatch.setattr(
            "code_tutor_agent.api.email.send_email",
            lambda *a, **k: direct_calls.append(a) or True,
        )
        ok, detail = mc.send_alert_email(["a@b.c"], "subj", "body")
        assert ok and detail.startswith("hub:")
        assert "initialize" in hub_calls and "tools/call" in hub_calls
        assert direct_calls == []  # hub 成功绝不回退直连

    def test_hub_http_error_fails_gracefully(self, monkeypatch):
        """hub HTTP 错误：返回 (False, hub: ...)，绝不抛异常（无兜底通道可回退）。"""
        import code_tutor_agent.monitoring.mail_client as mc

        self._setup_env(monkeypatch)

        def fake_rpc(payload, token, session_id=None):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", None, None)  # type: ignore[arg-type]

        monkeypatch.setattr(mc, "_rpc", fake_rpc)
        ok, detail = mc.send_alert_email(["a@b.c"], "subj", "body")
        assert not ok and detail == "hub: hub HTTP 401"

    def test_hub_is_error_fails(self, monkeypatch):
        """hub 工具 isError：视为发送失败，detail 带工具错误文本。"""
        import code_tutor_agent.monitoring.mail_client as mc

        self._setup_env(monkeypatch)

        def fake_rpc(payload, token, session_id=None):
            if payload.get("method") == "initialize":
                return {"jsonrpc": "2.0", "id": 1, "result": {}}, "sid-1"
            return {
                "jsonrpc": "2.0", "id": 2,
                "result": {"isError": True, "content": [{"type": "text", "text": "quota exhausted"}]},
            }, "sid-1"

        monkeypatch.setattr(mc, "_rpc", fake_rpc)
        ok, detail = mc.send_alert_email(["a@b.c"], "subj", "body")
        assert not ok and "quota exhausted" in detail

    def test_no_hub_short_circuit(self, monkeypatch):
        """hub 未配置：短路返回，连一次 HTTP 都不发。"""
        import code_tutor_agent.monitoring.mail_client as mc

        self._setup_env(monkeypatch, hub_token="")
        calls: list = []

        def fake_rpc(*a, **k):
            calls.append(a)
            return None, None

        monkeypatch.setattr(mc, "_rpc", fake_rpc)
        ok, detail = mc.send_alert_email(["a@b.c"], "subj", "body")
        assert not ok and detail == "hub: MCP_HUB_TOKEN not configured"
        assert calls == []  # 未配置时绝不发起请求

    def test_both_channels_down(self, monkeypatch):
        import code_tutor_agent.monitoring.mail_client as mc

        self._setup_env(monkeypatch)
        monkeypatch.setattr(mc, "_rpc", lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 401, "Unauthorized", None, None)))
        monkeypatch.setattr("code_tutor_agent.api.email.is_configured", lambda: False)
        ok, detail = mc.send_alert_email(["a@b.c"], "subj", "body")
        assert not ok

    def test_no_recipients_short_circuit(self, monkeypatch):
        import code_tutor_agent.monitoring.mail_client as mc

        assert mc.send_alert_email([], "subj", "body") == (False, "hub: no recipients")
