"""PostgreSQL 兼容层（2026-09-07 SQLite → PostgreSQL 迁移）。

背景：原 `db/database.py` 是 2617 行的裸 SQL DAO（sqlite3 风格：`?` 占位符、
`sqlite3.Row`（同时支持 int/str 索引）、`cursor.lastrowid`、TIMESTAMP 列按
'YYYY-MM-DD HH:MM:SS' 文本读写）。为了把 SQL 语句本体和全部调用点保持原样
（减少 diff、降低回归风险），本模块提供一个 sqlite3 兼容外观（adapter）：

- ``get_conn()`` 返回包装 psycopg3 连接池连接的 ``PGConnection``；
- ``PGCursor.execute`` 内部做 `?` → `%s` 占位符翻译（跳过字符串字面量里的 `?`）；
- 取行包装成 ``Row``（dict 子类，兼容 `row[0]` / `row["id"]` 两种索引），
  且把 ``datetime``/``date`` 转回 'YYYY-MM-DD HH:MM:SS' 文本（保持下游
  pydantic/前端对时间字符串的既有语义，与 SQLite 时代完全一致）；
- ``lastrowid`` 从 ``INSERT ... RETURNING id`` 捕获（调用点已补 RETURNING）。

时区语义：沿用原库约定——所有时间列为 **本地时间**（原 SQLite 用
``datetime('now','localtime')``，已整体替换为 ``LOCALTIMESTAMP``），非 UTC。
"""
from __future__ import annotations

import atexit
import datetime as _dt
import logging
import os
import threading
from typing import Any, Optional

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# ── 异常别名：让原 `except sqlite3.IntegrityError/OperationalError` 语义延续 ──
# （psycopg 的 DuplicateColumn 等归 ProgrammingError，DDL 幂等兜底处已改按 Exception 捕获）
IntegrityError = pg_errors.IntegrityError
OperationalError = pg_errors.OperationalError


def get_database_url() -> str:
    """PG 连接串（单一配置入口）。默认指向本机 docker compose 的 app-db 服务。"""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://code_tutor:code_tutor@localhost:5432/code_tutor",
    )


# ── 连接池（_with_conn 每次借还；psycopg_pool 的 close 即归还）──
_pool = None
_pool_lock = threading.Lock()
_POOL_MIN = int(os.getenv("CTA_PG_POOL_MIN", "1"))
_POOL_MAX = int(os.getenv("CTA_PG_POOL_MAX", "10"))


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                # 防误连守卫：pytest 里若 PG 测试库不可达（conftest 置位），
                # DB 类测试直接报清晰错误，绝不静默写进开发/生产库。
                if os.environ.get("CTA_PG_UNAVAILABLE") == "1":
                    raise RuntimeError(
                        "测试会话的 PostgreSQL 不可达（conftest 已探测失败）。"
                        "请先 docker compose -f docker/docker-compose.yml up -d app-db "
                        "并确保 DATABASE_URL 可连，再跑 DB 类测试。"
                    )
                from psycopg_pool import ConnectionPool

                kwargs: dict[str, Any] = {
                    # 不用 dict_row：重名/无名聚合列会被 dict 折叠（真库实测踩过）；
                    # 行包装在 PGCursor 里按 description 名称 + 位置元组双轨处理。
                    "autocommit": False,
                    # 沙箱/防火墙会静默丢弃到 5432 的 SYN，必须显式超时
                    "connect_timeout": int(os.getenv("CTA_PG_CONNECT_TIMEOUT", "5")),
                }
                # 测试隔离：conftest 提供一次性 schema，整个 pytest 会话共用
                schema = os.getenv("CTA_PG_SCHEMA")
                if schema:
                    kwargs["options"] = f"-c search_path={schema},public"
                _pool = ConnectionPool(
                    get_database_url(),
                    min_size=_POOL_MIN,
                    max_size=_POOL_MAX,
                    open=True,
                    kwargs=kwargs,
                )
                logger.info("PG connection pool ready (min=%d, max=%d, schema=%s)",
                            _POOL_MIN, _POOL_MAX, schema or "default")
                # 进程退出时归还/关闭全部池线程（否则解释器收尾报 pool 线程停不掉）
                atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    """进程退出时归还/关闭全部连接（可安全重复调用）。"""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


# ── SQL 翻译：`?` → `%s`（跳过单引号字面量内的 `?`）──

def translate_sql(sql: str) -> str:
    if "?" not in sql:
        return sql
    out: list[str] = []
    in_quote = False
    for ch in sql:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == "?" and not in_quote:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _cell(value: Any) -> Any:
    """datetime/date → 文本（与 SQLite 时代 TEXT 存储的读取语义一致）。"""
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


class Row(dict):
    """兼容 sqlite3.Row 的 dict 子类：同时支持 ``row["id"]`` 与 ``row[0]``。

    - 名称映射来自 cursor.description（重名列时后值覆盖前值，取 str 键时生效）；
    - **位置索引保存在独立元组**（``row[1]`` 永远取第 2 列）——这一点很关键：
      聚合查询常有多列同名/无名列，若用 dict.values() 当位置序，重复列会被
      dict 折叠导致 row[1] 越界（真库实测踩过）。
    - datetime/date 在构造时统一转回 'YYYY-MM-DD HH:MM:SS' 文本。
    """

    def __init__(self, mapping=None, positional=None):
        super().__init__(mapping or {})
        self._pos = tuple(positional) if positional is not None else tuple(self.values())
        for k, v in self.items():
            if isinstance(v, _dt.datetime) or isinstance(v, _dt.date):
                self[k] = _cell(v)
        self._pos = tuple(_cell(v) for v in self._pos)

    def __getitem__(self, key):
        if isinstance(key, int) and not isinstance(key, bool):
            return self._pos[key]
        return super().__getitem__(key)


def _wrap_row(raw, names=None) -> Optional[Row]:
    """把 psycopg 原生 tuple 行包装成 Row（dict_row 已弃用：重名/无名聚合列会被折叠）。"""
    if raw is None:
        return None
    mapping = dict(zip(names, raw)) if names else {}
    return Row(mapping, positional=raw)


class PGCursor:
    """包装 psycopg cursor，对齐 sqlite3.Cursor 的使用面。"""

    def __init__(self, cur):
        self._cur = cur
        self._lastrowid: Optional[int] = None

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    def execute(self, sql, params=None):
        self._lastrowid = None
        tsql = translate_sql(sql)
        self._cur.execute(tsql, params if params is not None else None)
        # INSERT ... RETURNING id → 捕获自增主键（lastrowid 语义）
        if self._cur.description is not None and "returning" in tsql.lower():
            row = self._cur.fetchone()
            if row is not None:
                names = [d.name for d in self._cur.description]
                if "id" in names:
                    self._lastrowid = row[names.index("id")]
        return self

    def executemany(self, sql, seq_of_params):
        self._lastrowid = None
        self._cur.executemany(translate_sql(sql), seq_of_params)
        return self

    def _names(self):
        """当前结果集的列名列表（无结果集返回 None）。"""
        return [d.name for d in self._cur.description] if self._cur.description else None

    def fetchone(self) -> Optional[Row]:
        return _wrap_row(self._cur.fetchone(), self._names())

    def fetchall(self) -> list[Row]:
        names = self._names()
        return [r for r in (_wrap_row(x, names) for x in self._cur.fetchall()) if r is not None]

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        names = self._names()
        for row in self._cur:
            wrapped = _wrap_row(row, names)
            if wrapped is not None:
                yield wrapped


class PGConnection:
    """包装池化 psycopg 连接：commit/rollback 透传，close = 归还池。

    用 pool.getconn()/putconn() 直取直还（pool.connection() 是上下文管理器，
    不能用于 _with_conn 的「函数式借还」模式）。
    """

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def cursor(self) -> PGCursor:
        return PGCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        """归还连接到池（先回滚未提交事务，避免下一个借用者见到脏状态）。"""
        try:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def get_conn() -> PGConnection:
    """借一个池化连接（原 _get_conn 的替代入口）。"""
    return PGConnection(_get_pool())


def ping() -> bool:
    """连通性探针（watcher / 启动自检用）。"""
    try:
        conn = get_conn()
        try:
            conn.cursor().execute("SELECT 1").fetchone()
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning("PG ping failed: %s", exc)
        return False
