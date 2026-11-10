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


class TestEditTraceFifoCap:
    """存储闸门（2026-09-09）：事件数 FIFO 上限 + 超大代码快照整条丢弃。"""

    def _ev(self, i: int) -> dict:
        return {"ts": 1000 + i, "type": "edit", "seq": i}

    def test_fifo_cap_keeps_latest_1000(self, tmp_db):
        from code_tutor_agent.db.database import MAX_EDIT_TRACE_EVENTS

        # 两次 flush 共 1200 条 → 只保留最近 1000 条（丢最旧 200 条）
        db.save_edit_trace("cap1", "default", [self._ev(i) for i in range(400)])
        db.save_edit_trace("cap1", "default", [self._ev(i) for i in range(400, 1200)])
        events = db.get_edit_trace("cap1")
        assert len(events) == MAX_EDIT_TRACE_EVENTS == 1000
        # 最旧的被裁掉：剩下的是 [200, 1199]
        seqs = [e["seq"] for e in events]
        assert seqs[0] == 200 and seqs[-1] == 1199
        assert seqs == sorted(seqs)  # 顺序保持

    def test_stress_2000_events_across_4_flushes(self, tmp_db):
        """压测：4 次 flush × 500 条 = 2000 条 → 稳定收敛到 1000，最新 flush 完整保留。"""
        for flush in range(4):
            base = flush * 500
            db.save_edit_trace("cap2", "default", [self._ev(base + i) for i in range(500)])
        events = db.get_edit_trace("cap2")
        assert len(events) == 1000
        seqs = [e["seq"] for e in events]
        assert seqs[-1] == 1999  # 最新事件永远在
        assert seqs[0] == 1000   # 最旧 1000 条被 FIFO 裁掉

    def test_cap_isolated_per_problem(self, tmp_db):
        """上限按 (session, problem) 行独立计量，不跨题互相挤占。"""
        from code_tutor_agent.db.database import MAX_EDIT_TRACE_EVENTS

        evs_a = [dict(self._ev(i), code=f"a{i}") for i in range(MAX_EDIT_TRACE_EVENTS + 100)]
        evs_b = [self._ev(i) for i in range(10)]
        db.save_edit_trace("cap3", "default", evs_a, problem_id="67")
        db.save_edit_trace("cap3", "default", evs_b, problem_id="68")
        assert len(db.get_edit_trace_by_problem("cap3", "67")) == MAX_EDIT_TRACE_EVENTS
        assert len(db.get_edit_trace_by_problem("cap3", "68")) == 10

    def test_oversized_code_snapshot_dropped(self, tmp_db):
        from code_tutor_agent.config import MAX_CODE_BYTES

        oversized = "a" * (MAX_CODE_BYTES + 1)
        events = [
            {"ts": 1, "type": "edit", "code": "ok\n"},
            {"ts": 2, "type": "edit", "code": oversized},          # 超大快照 → 整条丢弃
            {"ts": 3, "type": "idle", "idleMs": 5000},             # 无 code 字段，不受影响
        ]
        db.save_edit_trace("cap4", "default", events)
        out = db.get_edit_trace("cap4")
        assert [e["ts"] for e in out] == [1, 3]
        assert not any(e.get("code") == oversized for e in out)

    def test_oversized_code_at_pause_dropped(self, tmp_db):
        from code_tutor_agent.config import MAX_CODE_BYTES

        events = [
            {"ts": 1, "type": "idle", "code_at_pause": "x" * (MAX_CODE_BYTES + 1)},
            {"ts": 2, "type": "edit", "code": "ok\n"},
        ]
        db.save_edit_trace("cap5", "default", events)
        out = db.get_edit_trace("cap5")
        assert [e["ts"] for e in out] == [2]

    def test_all_events_dropped_still_writes_row(self, tmp_db):
        """整批都是超大事件：不抛错、落一行空事件（与既有 no-events 行为一致）。"""
        from code_tutor_agent.config import MAX_CODE_BYTES

        db.save_edit_trace("cap6", "default", [
            {"ts": 1, "type": "edit", "code": "x" * (MAX_CODE_BYTES + 1)},
        ])
        assert db.get_edit_trace("cap6") == []


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
        out = db.get_edit_trace("s5")
        # 全量方案：legacy 事件原样读回（均带 code，无 diff 链字段）
        assert out[0]["code"] == "a\n"
        assert "code_format" not in out[0] and "code_diff" not in out[0]

    def test_same_as_prev_inherits_last_code(self, tmp_db):
        # same_as_prev 事件不携带代码：reconstruct 时继承上一快照的 code（保证下游始终有全量）
        events = [
            {"ts": 1, "type": "edit", "code": "a\n", "problem_id": "67"},
            {"ts": 2, "type": "run", "same_as_prev": True, "problem_id": "67"},
        ]
        db.save_edit_trace("s6", "default", events)
        out = db.get_edit_trace("s6")
        assert out[1]["type"] == "run"
        assert out[1]["code"] == "a\n"  # 继承上一条 edit 的 code


class TestExtractForAnalysis:
    """覆盖 trace/extract.py 抽取层：去重、时间桶合并、stuck_segments、thrash。"""

    def _mk(self, base: str, new: str) -> str:
        import difflib
        a, b = base.split("\n"), new.split("\n")
        sm = difflib.SequenceMatcher(None, a, b)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            old_lines, new_lines = a[i1:i2], b[j1:j2]
            return f"# {i1}-{i2} -> {j1}-{j2}\n" + "\n".join(
                ["-" + ln for ln in old_lines] + ["+" + ln for ln in new_lines]
            )
        return ""

    def test_merges_time_buckets_and_dedup(self):
        from code_tutor_agent.trace.extract import extract_for_analysis

        v1 = "def f():\n    return 1\n"
        v2 = "def f():\n    return 2\n"
        # 连续 5 条 edit（同 code 仅时间不同，间隔 <400ms）→ 去重 + 合并只剩首尾
        events = []
        for i in range(5):
            events.append({
                "ts": 1000 + i * 100, "type": "edit",
                "code": v1 if i < 2 else v2, "problem_id": "67",
            })
        res = extract_for_analysis(events)
        # 去重后仅保留 v1(首) 与 v2(末)，merged 事件数 < 原始
        assert res.event_count_raw == 5
        assert res.event_count_kept < 5
        assert res.metrics["edit_count"] == 2  # 仅 2 个不同 code

    def test_stuck_segments_carry_code_and_dialogue(self):
        from code_tutor_agent.trace.extract import extract_for_analysis

        v1 = "x = 1\n"
        v2 = "x = 2\n"
        events = [
            {"ts": 1000, "type": "edit", "code": v1, "problem_id": "67"},
            {  # stuck 卡壳：带 code_at_pause + dialogue_before
                "ts": 2000, "type": "idle", "idleMs": 60000, "level": "stuck",
                "code_at_pause": v1, "dialogue_before": [{"role": "tutor", "content": "注意边界"}],
                "problem_id": "67",
            },
            {"ts": 80000, "type": "edit", "code": v2, "problem_id": "67"},
        ]
        res = extract_for_analysis(events)
        assert res.metrics["stuck_count"] == 1
        seg = res.stuck_segments[0]
        assert seg["code_at_pause"] == v1
        assert seg["pre_code"] == v1
        assert seg["post_code"] == v2
        assert seg["dialogue_before"][0]["content"] == "注意边界"

    def test_thrash_detected(self):
        from code_tutor_agent.trace.extract import extract_for_analysis

        # 连续 5 条 edit，code 互不相同（写一段删又写）→ thrash
        codes = [f"v{i}\n" for i in range(5)]
        events = [
            {"ts": 1000 + i * 100, "type": "edit", "code": codes[i], "problem_id": "67"}
            for i in range(5)
        ]
        res = extract_for_analysis(events)
        assert res.metrics["thrash_count"] >= 1

    def test_over_limit_coalesces_instead_of_prefix_truncate(self):
        from code_tutor_agent.trace.extract import extract_for_analysis

        # 100 条独立 edit（间隔 10s，不会被 400ms 时间桶合并）
        events = [
            {"ts": i * 10_000, "type": "edit", "code": f"x = {i}\n", "problem_id": "67"}
            for i in range(100)
        ]
        res = extract_for_analysis(events)
        assert res.metrics["event_count_merged"] == 100
        assert res.metrics["event_count_rendered"] <= 80
        # 尾部不再被整段丢掉：最后一条 code 的净变化仍出现在时间线里
        assert "x = 99" in res.timeline_text
        # 归并后仍从会话开头起算（首条复合事件含两个 [edit] 标签）
        assert res.timeline_text.startswith("[edit] → [edit]")

    def test_budget_trim_keeps_full_span(self):
        from code_tutor_agent.trace.extract import extract_for_analysis

        events = [
            {"ts": i * 10_000, "type": "edit", "code": f"x = {i}\n", "problem_id": "67"}
            for i in range(20)
        ]
        res = extract_for_analysis(events, max_tokens=50)
        assert res.metrics["event_count_rendered"] < res.metrics["event_count_merged"]
        # 预算裁剪走两两归并：条数减少但会话尾部仍在
        assert "x = 19" in res.timeline_text


class TestCompositeTimeline:
    """覆盖 preprocess 对复合事件（预算裁剪产物）的渲染。"""

    def test_composite_renders_labels_and_net_diff(self):
        from code_tutor_agent.trace.preprocess import build_dialogue_timeline

        comp = {
            "type": "composite",
            "ts": 3000,
            "code": "b\n",
            "parts": [
                {"type": "edit", "code": "a\n",
                 "dialogue_before": [{"role": "tutor", "content": "提示A"}]},
                {"type": "run"},
                {"type": "edit", "change": "insert", "code": "b\n",
                 "dialogue_before": [{"role": "tutor", "content": "提示A"}]},
            ],
        }
        text = build_dialogue_timeline([comp], with_diffs=True)
        assert "[edit] → [运行] → [edit/insert]" in text
        assert text.count("提示A") == 1      # 对话按内容去重
        assert "+ b" in text                 # 跨度净 diff（上一条保留事件 → 跨度末端）


class TestPurgeTraceData:
    def test_purge_removes_old(self, tmp_db):
        # 插入一条 31 天前的 edit_traces（手动设旧 updated_at），再 purge(days=30) 应删除
        import sqlite3
        db.save_edit_trace("sX", "default", [{"ts": 1, "type": "edit", "code": "a"}])
        # 把它改成 31 天前
        db._with_conn(lambda cur: cur.execute(
            "UPDATE edit_traces SET updated_at = LOCALTIMESTAMP - INTERVAL '31 days' "
            "WHERE session_id='sX'"
        ))
        assert len(db.get_edit_trace("sX")) == 1
        stats = db.purge_trace_data(days=30)
        assert stats.get("edit_traces", 0) >= 1
        assert db.get_edit_trace("sX") == []

    def test_purge_keeps_recent(self, tmp_db):
        # 当天的记录 purge(days=30) 不应被删
        db.save_edit_trace("sY", "default", [{"ts": 1, "type": "edit", "code": "b"}])
        stats = db.purge_trace_data(days=30)
        assert stats.get("edit_traces", 0) == 0
        assert len(db.get_edit_trace("sY")) == 1


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
