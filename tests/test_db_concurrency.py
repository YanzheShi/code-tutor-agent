"""并发健壮性回归（PG 版，2026-09-07 随 SQLite→PG 迁移改写）。

SQLite 时代背景：判题主链路压测随机断裂
    agent_judge_node → get_problem_by_id → `PRAGMA journal_mode=WAL`
    → OperationalError: database is locked → /submit 500 → 会话永久死锁

迁移到 PostgreSQL 后：
- WAL / busy_timeout / 库级写锁排队等 3 条 SQLite 内部实现用例已删除——
  PG 走 MVCC + 连接池，`database is locked` 这一失败模式架构性消失；
- 保留 3 条仍然有效的行为契约：
  1. 写-写并发不互相击穿（PG MVCC 下天然成立，作回归护栏）；
  2. save_problem 撞 title UNIQUE 的竞态兜底（复用旧题而不是 500）；
  3. judge 节点对基础设施异常零击穿（status=error 保活，会话不死锁）。

表隔离：共享测试 schema + conftest 的 _pg_clean_tables 每测试后清表。
"""

from __future__ import annotations

import threading

import pytest
import psycopg

from code_tutor_agent.db import database as db
from code_tutor_agent.nodes.agent_judge import agent_judge_node
from code_tutor_agent.schemas.state import (
    ProblemMeta,
    SessionState,
    Submission,
)


def test_write_write_contention_succeeds():
    """多线程同时写库应全部成功（PG MVCC：无库级写锁，无需排队等待）。

    这是 SQLite 时代 test_with_conn_waits_for_lock_instead_of_failing 的
    PG 对应物：当年 worker 会被写锁阻塞 1 秒以上，如今 4 个并发写
    理应立即完成、零失败。
    """
    db._with_conn(lambda c: c.execute("DROP TABLE IF EXISTS _ct_scratch"))
    db._with_conn(lambda c: c.execute("CREATE TABLE _ct_scratch (id INT PRIMARY KEY, v TEXT)"))

    results: list[tuple[str, object]] = []

    def worker(i: int) -> None:
        try:
            db._with_conn(
                lambda c: c.execute(
                    "INSERT INTO _ct_scratch (id, v) VALUES (%s, %s)", (i, f"w{i}")
                )
            )
            results.append(("ok", i))
        except Exception as exc:  # pragma: no cover
            results.append(("err", str(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not any(t.is_alive() for t in threads), "存在未退出的 worker 线程"
    errs = [r for r in results if r[0] == "err"]
    assert not errs, f"并发写入出现失败：{errs}"

    count = db._with_conn(lambda c: c.execute("SELECT COUNT(*) FROM _ct_scratch").fetchone()[0])
    assert count == 4, "4 个并发写入应全部落库"


def test_save_problem_race_falls_back_to_reuse():
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


def test_judge_node_survives_db_error(monkeypatch):
    """前置加载抛 DB 异常时返回 status=error 保活，而不是让异常击穿会话。

    status=error 由 agent_judge_router 路由到 wait_for_submit_node，
    会话保持可继续（用户改代码再提交即可）；异常击穿则会话永久死锁。
    """
    def _boom(_state):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(
        "code_tutor_agent.nodes.agent_judge._resolve_inputs", _boom
    )

    result = agent_judge_node(_make_state())

    assert result["status"] == "error"
    assert "connection refused" in result["error_message"]
