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


class TestReconstructEditTrace:
    """覆盖：diff 事件按序重建全量快照、链断丢弃、旧数据原样通过。"""

    def _diff(self, old: str, new: str) -> str:
        """用与前端同构的方式生成 diff 文本（# 区间 / -旧 / +新）。"""
        import difflib

        a, b = old.split("\n"), new.split("\n")
        sm = difflib.SequenceMatcher(None, a, b)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            old_lines, new_lines = a[i1:i2], b[j1:j2]
            return f"# {i1}-{i2} -> {j1}-{j2}\n" + "\n".join(
                ["-" + ln for ln in old_lines] + ["+" + ln for ln in new_lines]
            )
        return ""

    def test_reconstructs_diff_chain(self, tmp_db):
        base = "a\nb\nc\n"
        mid = "a\nb\nc\nd\n"
        final = "a\nx\nc\nd\n"
        events = [
            {"ts": 1, "type": "edit", "code": base, "problem_id": "67"},
            {"ts": 2, "type": "edit", "code_format": "diff",
             "code_diff": self._diff(base, mid), "problem_id": "67"},
            {"ts": 3, "type": "edit", "code_format": "diff",
             "code_diff": self._diff(mid, final), "problem_id": "67"},
        ]
        db.save_edit_trace("s3", "default", events)
        out = db.get_edit_trace("s3")
        assert [e["type"] for e in out] == ["edit", "edit", "edit"]
        assert out[0]["code"] == base
        assert out[1]["code"] == mid
        assert out[2]["code"] == final
        # diff 专用字段被移除，下游看到的是全量
        assert "code_format" not in out[1] and "code_diff" not in out[1]

    def test_dropped_when_chain_broken(self, tmp_db):
        # 首条就是 diff（前面无全量基准）→ 无法重建 → 丢弃
        events = [
            {"ts": 1, "type": "edit", "code_format": "diff",
             "code_diff": "# 0-1 -> 0-1\n+x\n", "problem_id": "67"},
            {"ts": 2, "type": "run", "code": "x\n", "problem_id": "67"},
        ]
        db.save_edit_trace("s4", "default", events)
        out = db.get_edit_trace("s4")
        assert len(out) == 1 and out[0]["type"] == "run"

    def test_legacy_events_passthrough(self, tmp_db):
        events = [{"ts": 1, "type": "edit", "code": "a\n", "problem_id": "67"}]
        db.save_edit_trace("s5", "default", events)
        assert db.get_edit_trace("s5") == events

    def test_same_as_prev_keeps_last_code(self, tmp_db):
        # same_as_prev 事件不携带代码：last_code 保持上一快照
        events = [
            {"ts": 1, "type": "edit", "code": "a\n", "problem_id": "67"},
            {"ts": 2, "type": "run", "same_as_prev": True, "problem_id": "67"},
        ]
        db.save_edit_trace("s6", "default", events)
        out = db.get_edit_trace("s6")
        assert out[1]["type"] == "run" and "code" not in out[1]


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
