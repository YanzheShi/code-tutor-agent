"""SQLite 并发健壮性回归（2026-09-04 回归实测发现）。

背景：判题主链路在压测中随机断裂，表现为
    agent_judge_node → get_problem_by_id → _get_conn()
    → `PRAGMA journal_mode=WAL` → OperationalError: database is locked
    → 节点抛异常 → /submit 500 → 状态不落盘、会话失去挂起节点 → /run 一律 400

根因两条，本文件各锁一条：
1. `_get_conn()` 把「数据库级持久属性」WAL 当成「每次连接都要设」的会话属性，
   而该 pragma 需要短暂独占锁，后台线程持写事务时必炸。
2. 判题节点对基础设施异常零容错，异常直接击穿，会话永久死锁（不可恢复→可恢复）。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from code_tutor_agent.db import database as db
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.schemas.state import (
    ProblemMeta,
    SessionState,
    Submission,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """把 database 模块指向临时库，并重置 WAL 标志（每用例独立）。"""
    path = tmp_path / "test_concurrency.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_WAL_READY", False)
    return path


def _init_schema(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()


def test_with_conn_waits_for_lock_instead_of_failing(tmp_db):
    """写-写争用时要按 busy_timeout 排队等待，而不是立刻抛 locked。

    注：不要写成「持锁时执行 PRAGMA journal_mode=WAL 必须抛错」——实测当库已是
    WAL 时该 pragma 是 no-op，根本不会抛（2026-09-04 探针验证），那种断言永远绿。
    真正决定行为的是 busy_timeout：持锁方释放后，等待方应能接着写成功。
    """
    _init_schema(tmp_db)

    holder = sqlite3.connect(str(tmp_db))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (v) VALUES ('holding')")

    result: dict = {}

    def worker() -> None:
        try:
            db._with_conn(lambda cur: cur.execute("INSERT INTO t (v) VALUES ('worker')"))
            result["ok"] = True
        except sqlite3.OperationalError as exc:  # pragma: no cover
            result["err"] = str(exc)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(1.0)  # worker 此刻应正阻塞在锁上
    holder.rollback()
    holder.close()
    t.join(timeout=20)

    assert not t.is_alive(), "worker 线程未退出"
    assert result.get("ok") is True, f"等待方写入失败：{result.get('err')}"
    left = db._with_conn(lambda cur: cur.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    assert left == 1, "持锁方回滚后只应保留 worker 写入的 1 行"


def test_get_conn_sets_wal_only_once(tmp_db):
    """WAL pragma 每进程只发一次：第二次起即使库被独占也不再触碰该 pragma。"""
    _init_schema(tmp_db)

    first = db._get_conn()
    first.close()
    assert db._WAL_READY is True
    assert first is not None

    # WAL 已就绪后，即便另一连接独占写锁，新建连接也不应抛错
    holder = sqlite3.connect(str(tmp_db))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (v) VALUES ('x')")
    try:
        conn = db._get_conn()
        conn.close()
    finally:
        holder.rollback()
        holder.close()


def test_get_conn_sets_busy_timeout(tmp_db):
    """连接必须带 busy_timeout，让写-写争用排队而不是立即失败。"""
    _init_schema(tmp_db)
    conn = db._get_conn()
    try:
        # PRAGMA busy_timeout 返回当前值（ms）
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db._BUSY_TIMEOUT_MS
    finally:
        conn.close()


def _make_state() -> SessionState:
    return SessionState(
        session_id="test-db-error",
        mode="agent",
        status="awaiting_submit",
        problem=ProblemMeta(
            problem_id=1,
            title="测试题",
            topic="数组",
            difficulty="easy",
            description="测试",
            starter_code="class Solution:\n    def solve(self):\n        pass",
        ),
        submissions=[
            Submission(index=1, code="class Solution:\n    def solve(self):\n        return 42",
                       verdict="", timestamp="2026-09-04T12:00:00"),
        ],
    )


def test_save_problem_race_falls_back_to_reuse(tmp_db):
    """并发落库竞态兜底：先查后插撞 title UNIQUE 时复用旧题而不是 500。

    save_problem 是"先 SELECT 查重，再 INSERT"，两步之间不是原子的。
    两个用户同时落同一道题时双方都查不到对方未提交的行，后提交者撞
    title UNIQUE。修复后 INSERT 撞 IntegrityError 应回查旧 id 复用。

    测试用确定性方式触发同一路径：同 title、不同 starter_code/source_url，
    绕开查询路径去重，让 INSERT 必然撞 UNIQUE。
    """
    base = {
        "title": "两数之和",
        "topic": "数组",
        "difficulty": "easy",
        "description": "返回两数下标",
        "test_cases": [{"input": "[2,7,11,15]\n9", "output": "[0,1]"}],
        "starter_code": "class Solution:\n    def twoSum(self, nums, target):\n        pass",
        "source_url": "https://leetcode.com/problems/two-sum/",
    }
    pid_first, reused_first = db.save_problem(dict(base))
    assert reused_first is False

    # 不同内容但同 title —— 查询路径去重全部 miss，INSERT 撞 title UNIQUE
    variant = dict(base)
    variant["starter_code"] = "class Solution:\n    def twoSum(self, nums, target):\n        # 另一套 starter\n        pass"
    variant["source_url"] = "https://example.com/other/two-sum/"
    pid_fallback, reused_fallback = db.save_problem(variant)

    assert reused_fallback is True
    assert pid_fallback == pid_first, "竞态撞 UNIQUE 后应复用旧题 id"

    # 库里始终只有一行，不会出现半截写入
    count = db._with_conn(
        lambda cur: cur.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    )
    assert count == 1


def test_judge_node_survives_db_error(monkeypatch):
    """前置加载抛 DB 异常时返回 status=error 保活，而不是让异常击穿会话。

    status=error 由 agent_judge_router 路由到 wait_for_submit_node，
    会话保持可继续（用户改代码再提交即可）；异常击穿则会话永久死锁。
    """
    def _boom(_state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "code_tutor_agent.nodes.agent_judge._resolve_inputs", _boom
    )

    result = agent_judge_node(_make_state())

    assert result["status"] == "error"
    assert "database is locked" in result["error_message"]
