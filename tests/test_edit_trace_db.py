"""Test: edit_traces 落库 + error_modes 聚合落库（DB 层，使用临时库，不碰 dev DB）。

覆盖：save_edit_trace 累计、get_edit_trace 读回、apply_error_mode_deltas 经 DBProfile 往返。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from code_tutor_agent.db import database as db


@pytest.fixture
def tmp_db(monkeypatch):
    """把 DB_PATH 指到临时文件并初始化表结构。"""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "test_code_tutor.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    # 清理由 tmp 目录负责；不在此删除，避免跨平台权限问题


class TestEditTraceStore:
    def test_save_accumulates_across_flushes(self, tmp_db):
        db.save_edit_trace("s1", "default", [{"ts": 1, "type": "edit"}])
        db.save_edit_trace("s1", "default", [{"ts": 2, "type": "idle", "idleMs": 5000}])
        events = db.get_edit_trace("s1")
        assert len(events) == 2
        assert events[0]["type"] == "edit"
        assert events[1]["idleMs"] == 5000

    def test_get_missing_returns_empty(self, tmp_db):
        assert db.get_edit_trace("nope") == []

    def test_user_id_is_upserted(self, tmp_db):
        db.save_edit_trace("s2", "alice", [{"ts": 1, "type": "run"}])
        # 第二次带不同 user_id 不应丢失事件
        db.save_edit_trace("s2", "alice", [{"ts": 2, "type": "submit"}])
        assert len(db.get_edit_trace("s2")) == 2


class TestErrorModeDeltasDB:
    def test_apply_roundtrip_through_profile(self, tmp_db):
        from code_tutor_agent.profile.weakness import ErrorModeDelta

        deltas = [ErrorModeDelta(dim="perf", tag="tle_brute", delta_count=2, severity=0.6)]
        out = db.apply_error_mode_deltas("default", deltas, verdict_boost=False)

        # 落库后从 profile 读回应一致
        saved = db.get_profile("default").error_modes
        assert saved["perf"]["tle_brute"]["count"] == out["perf"]["tle_brute"]["count"]
        assert saved["perf"]["tle_brute"]["severity"] == 0.24  # 0*0.6 + 0.6*0.4

    def test_verdict_boost_roundtrip(self, tmp_db):
        from code_tutor_agent.profile.weakness import ErrorModeDelta

        db.apply_error_mode_deltas(
            "default",
            [ErrorModeDelta(dim="correctness", tag="boundary", delta_count=10, severity=0.5)],
            verdict_boost=False,
        )
        # 判题失败补充 feeder ×1.3
        db.apply_error_mode_deltas(
            "default",
            [ErrorModeDelta(dim="correctness", tag="boundary", delta_count=10, severity=0.5)],
            verdict_boost=True,
        )
        saved = db.get_profile("default").error_modes
        # 第一次: count=10, sev=0.2; 第二次(boost): count=10*0.85 + round(13)=13 → 21.5
        assert saved["correctness"]["boundary"]["count"] == 21.5
        assert saved["correctness"]["boundary"]["severity"] == 0.38
