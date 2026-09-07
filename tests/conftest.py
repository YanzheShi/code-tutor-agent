"""全局测试配置。

Rate-limit 桶隔离（防滥用改造配套）：auth 的 IP 限流桶是模块级内存态，
整包跑时跨测试累积会把后续走 TestClient 注册的测试顶到 429。
每条测试前后自动清桶；限流行为本身由 test_auth_multitenant 专项覆盖。

PostgreSQL 测试隔离（2026-09-07 迁移配套）：
- 会话启动时在 DATABASE_URL 指向的库上创建一次性 schema（CTA_PG_SCHEMA），
  所有 DB 类测试的建表/读写都落在该 schema 里，会话结束 DROP CASCADE；
- PG 不可达时置 CTA_PG_UNAVAILABLE=1，pg_compat 的守卫会让 DB 类测试
  直接报清晰错误（不会静默写进开发/生产库）；
- 旧测试里的 CTA_DB_PATH 环境变量已无效果（SQLite 已下线），无需清理。
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    from code_tutor_agent.api import auth as auth_mod

    auth_mod._RATE_BUCKETS.clear()
    yield
    auth_mod._RATE_BUCKETS.clear()


@pytest.fixture(scope="session", autouse=True)
def _pg_test_db():
    """一次性测试 schema：建表隔离 + 会话结束清理 + 不可达时防误连置位。"""
    schema = f"cta_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        import psycopg

        from code_tutor_agent.db.pg_compat import get_database_url

        with psycopg.connect(get_database_url(), autocommit=True, connect_timeout=3) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        os.environ["CTA_PG_SCHEMA"] = schema
        os.environ.pop("CTA_PG_UNAVAILABLE", None)
        yield schema
    except Exception:
        os.environ["CTA_PG_UNAVAILABLE"] = "1"
        yield None
    finally:
        if os.environ.get("CTA_PG_SCHEMA") == schema:
            os.environ.pop("CTA_PG_SCHEMA", None)
            try:
                from code_tutor_agent.db import pg_compat

                pg_compat.close_pool()
                import psycopg

                from code_tutor_agent.db.pg_compat import get_database_url

                with psycopg.connect(get_database_url(), autocommit=True, connect_timeout=3) as conn:
                    conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            except Exception:
                pass


# LangGraph checkpointer 的内部表（按需重建成本低，但跨测试保留无意义数据
# 会污染 list_sessions 类断言；一并清空）——PG 版统一直接清，避免遗漏。
_KEEP_TABLES: set[str] = set()


@pytest.fixture(autouse=True)
def _pg_clean_tables(_pg_test_db):
    """每条测试结束后清空测试 schema 里的全部业务表。

    SQLite 时代每条测试用独立临时库天然隔离；迁到共享 schema 后必须显式清，
    否则前一条测试的 session_activity/problems 等行会泄漏进后续断言
    （实测 count == 3 变 5）。PG 不可达时静默跳过，由守卫负责报错。
    """
    schema = os.environ.get("CTA_PG_SCHEMA")
    if not schema:
        yield
        return
    yield
    try:
        import psycopg

        from code_tutor_agent.db.pg_compat import get_database_url

        with psycopg.connect(get_database_url(), autocommit=True, connect_timeout=3) as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
            ).fetchall()
            targets = [r[0] for r in rows if r[0] not in _KEEP_TABLES]
            if targets:
                conn.execute(
                    "SET lock_timeout = '3s'; TRUNCATE TABLE "
                    + ", ".join(f'"{schema}"."{t}"' for t in targets)
                    + " CASCADE"
                )
    except Exception:
        pass
