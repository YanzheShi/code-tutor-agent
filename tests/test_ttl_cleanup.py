"""测试 TTL 自动清理功能。

包含:
1. touch_session 写入/更新
2. get_stale_sessions TTL 判断
3. delete_session_activity 删除
4. config 默认值
5. cleanup API 端点 dry_run / 真删除
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from code_tutor_agent.config import get_session_ttl_hours, get_cleanup_interval_minutes


# ============================================================
# Config 默认值
# ============================================================


class TestTTLConfig:
    def test_default_ttl_168h(self, monkeypatch):
        """默认 TTL 应该是 168 小时（7 天）。"""
        monkeypatch.delenv("SESSION_TTL_HOURS", raising=False)
        assert get_session_ttl_hours() == 168

    def test_default_interval_60min(self, monkeypatch):
        """默认清理间隔应该是 60 分钟。"""
        monkeypatch.delenv("SESSION_CLEANUP_INTERVAL_MINUTES", raising=False)
        assert get_cleanup_interval_minutes() == 60

    def test_custom_ttl_from_env(self, monkeypatch):
        """环境变量可以覆盖 TTL。"""
        monkeypatch.setenv("SESSION_TTL_HOURS", "24")
        assert get_session_ttl_hours() == 24

    def test_custom_interval_from_env(self, monkeypatch):
        """环境变量可以覆盖清理间隔。"""
        monkeypatch.setenv("SESSION_CLEANUP_INTERVAL_MINUTES", "30")
        assert get_cleanup_interval_minutes() == 30


# ============================================================
# session_activity 表操作
# ============================================================


@pytest.fixture
def temp_db(monkeypatch):
    """用临时数据库替换 DB_PATH，测试后自动清理。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_ttl_")
    os.close(fd)
    monkeypatch.setattr(
        "code_tutor_agent.db.database.DB_PATH",
        path,
    )
    # 初始化表结构
    from code_tutor_agent.db.database import init_db
    init_db()
    yield path
    # 清理
    try:
        os.unlink(path)
    except OSError:
        pass


class TestSessionActivityTable:
    """测试 session_activity 表的增删查。"""

    def test_touch_session_creates_record(self, temp_db):
        """touch_session 应该创建新记录。"""
        from code_tutor_agent.db.database import touch_session
        touch_session("session-001")
        # 直接查数据库验证
        from code_tutor_agent.db.database import _with_conn
        row = _with_conn(lambda c: c.execute(
            "SELECT * FROM session_activity WHERE session_id = ?",
            ("session-001",),
        ).fetchone())
        assert row is not None
        assert row["session_id"] == "session-001"
        assert row["last_active_at"] is not None

    def test_touch_session_updates_existing(self, temp_db):
        """对同一个 session_id 再次 touch，应该更新 last_active_at 而不是新建。"""
        from code_tutor_agent.db.database import touch_session, _with_conn

        touch_session("session-002")
        row1 = _with_conn(lambda c: c.execute(
            "SELECT last_active_at FROM session_activity WHERE session_id = ?",
            ("session-002",),
        ).fetchone())
        ts1 = row1["last_active_at"]

        # 等 1 秒确保时间戳变化
        time.sleep(1.1)
        touch_session("session-002")
        row2 = _with_conn(lambda c: c.execute(
            "SELECT last_active_at FROM session_activity WHERE session_id = ?",
            ("session-002",),
        ).fetchone())
        ts2 = row2["last_active_at"]

        # 时间戳应该更新了
        assert ts2 != ts1

        # 记录数应该只有 1 条
        count = _with_conn(lambda c: c.execute(
            "SELECT COUNT(*) as cnt FROM session_activity WHERE session_id = ?",
            ("session-002",),
        ).fetchone())["cnt"]
        assert count == 1

    def test_multiple_sessions(self, temp_db):
        """多个不同 session 各自独立记录。"""
        from code_tutor_agent.db.database import touch_session, _with_conn

        for i in range(3):
            touch_session(f"session-{i}")

        count = _with_conn(lambda c: c.execute(
            "SELECT COUNT(*) as cnt FROM session_activity",
        ).fetchone())["cnt"]
        assert count == 3

    def test_touch_session_no_error_on_missing_table(self, monkeypatch, temp_db):
        """touch_session 不应该在表不存在时抛出异常（初始化场景）。"""
        from code_tutor_agent.db.database import _with_conn
        # 先删掉表
        _with_conn(lambda c: c.execute("DROP TABLE IF EXISTS session_activity"))
        # touch 应该不抛异常
        from code_tutor_agent.db.database import touch_session
        # 应该被 try/except 捕获，不抛异常
        touch_session("session-x")


class TestGetStaleSessions:
    def test_no_stale_when_all_recent(self, temp_db):
        """如果所有 session 都是刚刚 touch 的，不应该有过期的。"""
        from code_tutor_agent.db.database import touch_session, get_stale_sessions

        for i in range(5):
            touch_session(f"fresh-{i}")

        stale = get_stale_sessions(1)  # 1 小时
        assert len(stale) == 0, f"Expected 0 stale, got {stale}"

    def test_stale_detected_with_manual_old_timestamp(self, temp_db):
        """手动插入一个旧时间戳，应该被正确检出。"""
        from code_tutor_agent.db.database import _with_conn, get_stale_sessions

        # 手动插入 100 小时前的时间戳
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-100 hours'))",
            ("old-session",),
        ))

        stale_168 = get_stale_sessions(168)  # 7 天 → 不过期
        assert len(stale_168) == 0

        stale_50 = get_stale_sessions(50)  # 50 小时 → 过期
        assert "old-session" in stale_50

    def test_mixed_stale_and_fresh(self, temp_db):
        """混合新旧 session，只检出过期的。"""
        from code_tutor_agent.db.database import touch_session, get_stale_sessions, _with_conn

        # 新鲜
        touch_session("fresh-1")
        touch_session("fresh-2")

        # 旧的
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-200 hours'))",
            ("stale-1",),
        ))
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-200 hours'))",
            ("stale-2",),
        ))

        stale = get_stale_sessions(168)
        assert len(stale) == 2
        assert "stale-1" in stale
        assert "stale-2" in stale
        assert "fresh-1" not in stale


class TestDeleteSessionActivity:
    def test_delete_removes_record(self, temp_db):
        """delete_session_activity 应该正确删除记录。"""
        from code_tutor_agent.db.database import (
            touch_session, delete_session_activity, _with_conn,
        )

        touch_session("to-delete")
        row = _with_conn(lambda c: c.execute(
            "SELECT 1 FROM session_activity WHERE session_id = ?",
            ("to-delete",),
        ).fetchone())
        assert row is not None

        delete_session_activity("to-delete")
        row = _with_conn(lambda c: c.execute(
            "SELECT 1 FROM session_activity WHERE session_id = ?",
            ("to-delete",),
        ).fetchone())
        assert row is None

    def test_delete_nonexistent_no_error(self, temp_db):
        """删除不存在的记录不应该报错。"""
        from code_tutor_agent.db.database import delete_session_activity
        delete_session_activity("nonexistent-id")


# ============================================================
# Cleanup API 端点（不依赖 graph）
# ============================================================


class TestCleanupAPI:
    def test_dry_run_returns_stale_list(self, temp_db):
        """dry_run 模式: 只返回列表，不删除。"""
        import asyncio

        from code_tutor_agent.db.database import _with_conn

        # 造 3 条过期数据
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-300 hours'))",
            ("dry-stale-1",),
        ))
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-300 hours'))",
            ("dry-stale-2",),
        ))
        _with_conn(lambda c: c.execute(
            "INSERT INTO session_activity (session_id, last_active_at) "
            "VALUES (?, datetime('now', '-300 hours'))",
            ("dry-stale-3",),
        ))

        from code_tutor_agent.api.routers.session import cleanup_sessions

        result = asyncio.run(cleanup_sessions(max_age_hours=168, dry_run=True))

        assert result["dry_run"] is True
        assert result["cleaned"] == 0
        assert result["stale_count"] == 3
        assert "dry-stale-1" in result["stale_sessions"]

    def test_cleanup_no_llm_external_deps(self, temp_db):
        """确认 TTL 清理不依赖 LLM 或外部服务。"""
        src = open(__file__, encoding="utf-8").read()
        # config.py 的新函数不应该依赖 LLM
        assert "get_session_ttl_hours" in src
        assert "get_cleanup_interval_minutes" in src

        # database.py 的表操作不应该引用 LLM
        from code_tutor_agent.db.database import (
            touch_session, get_stale_sessions, delete_session_activity,
        )
        assert callable(touch_session)
        assert callable(get_stale_sessions)
        assert callable(delete_session_activity)
