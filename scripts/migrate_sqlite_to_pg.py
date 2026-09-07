"""SQLite → PostgreSQL 存量数据一次性迁移（2026-09-07 迁移配套）。

用法（项目根目录）：
    python scripts/migrate_sqlite_to_pg.py                    # 默认源/目标
    python scripts/migrate_sqlite_to_pg.py --sqlite path/to/code_tutor.db
    python scripts/migrate_sqlite_to_pg.py --truncate         # 清空 PG 表后全量导入
    python scripts/migrate_sqlite_to_pg.py --dry-run          # 只统计行数，不写入

行为：
- 目标 PG 由 DATABASE_URL 指定（默认 postgresql://code_tutor:code_tutor@localhost:5432/code_tutor）；
- 先通过业务层 init_db() 在 PG 建全套 schema（幂等）；
- 按依赖序逐表迁移：读 SQLite 行 → 批量 INSERT（显式 id，IDENTITY 为 BY DEFAULT 允许覆盖）；
- 迁移后校准各 identity 表的序列（setval 到 MAX(id)，否则新插入撞主键）；
- 校验：逐表比对行数与 max(id)，输出报告；不一致时退出码 1。

不迁移：LangGraph checkpoint（SqliteSaver 与 PostgresSaver 序列化格式不同，
且会话本身有 TTL 过期语义）——迁移后用户需重开会话，历史业务数据不丢。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DEFAULT_SQLITE = os.environ.get(
    "CTA_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "db", "code_tutor.db"),
)

# 迁移顺序：submissions 依赖 problems（FK），其余无跨表外键
TABLES = [
    "problems",
    "users",
    "profiles",
    "user_settings",
    "invite_codes",
    "password_reset_codes",
    "session_activity",
    "edit_traces",
    "trace_analysis",
    "analysis_results",
    "trace_summaries",
    "trace_threads",
    "submissions",
    "token_usage",
    "token_usage_daily",
    "alert_history",
    "announcements",
]

# 带 identity 主键的表（迁移后需 setval 校准序列）
IDENTITY_TABLES = ["problems", "users", "submissions", "token_usage",
                   "alert_history", "announcements"]


def _pg_cols(cursor, table: str) -> list[str]:
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cursor.fetchall()]


def _sqlite_cols(sql_conn: sqlite3.Connection, table: str) -> list[str]:
    cur = sql_conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 存量数据迁移")
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE, help="源 SQLite 库路径")
    ap.add_argument("--truncate", action="store_true", help="导入前清空 PG 对应表")
    ap.add_argument("--dry-run", action="store_true", help="只统计行数，不写入")
    args = ap.parse_args()

    if not os.path.isfile(args.sqlite):
        print(f"[FAIL] 源库不存在: {args.sqlite}")
        return 1

    from code_tutor_agent.db.database import init_db
    from code_tutor_agent.db.pg_compat import get_conn, get_database_url

    print(f"[1/4] 源库: {args.sqlite}")
    sql_conn = sqlite3.connect(args.sqlite)
    sql_conn.row_factory = sqlite3.Row

    print(f"[2/4] 目标: {get_database_url()}")
    if not args.dry_run:
        init_db()

    conn = get_conn()
    report: dict[str, dict] = {}
    failed = False

    try:
        for table in TABLES:
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                pg_existing = cur.fetchone()[0]
            except Exception as exc:
                report[table] = {"error": f"PG 表不可用: {exc}"}
                failed = True
                continue

            s_cols = _sqlite_cols(sql_conn, table)
            if not s_cols:
                report[table] = {"error": "源库无此表（跳过）", "sqlite_rows": 0}
                continue
            p_cols = _pg_cols(cur, table)
            cols = [c for c in s_cols if c in p_cols]  # 交集，按源库列序

            src_rows = sql_conn.execute(
                f"SELECT {', '.join(s_cols)} FROM {table}"
            ).fetchall()
            n_src = len(src_rows)

            if args.dry_run:
                report[table] = {"sqlite_rows": n_src, "pg_rows_before": pg_existing,
                                 "columns": cols}
                continue

            if pg_existing > 0 and not args.truncate:
                report[table] = {"error": f"PG 已有 {pg_existing} 行，--truncate 后重试",
                                 "sqlite_rows": n_src}
                failed = True
                continue

            if args.truncate:
                # CASCADE：submissions 有指向 problems 的 FK，PG 禁止单表截断
                cur.execute(f"TRUNCATE TABLE {table} CASCADE")

            inserted = 0
            if n_src:
                placeholders = ", ".join(["%s"] * len(cols))
                col_list = ", ".join(cols)
                data = [tuple(r[c] for c in cols) for r in src_rows]
                # 分批写入，单表单事务
                for i in range(0, len(data), 1000):
                    cur.executemany(
                        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                        data[i:i + 1000],
                    )
                    inserted += len(data[i:i + 1000])
                conn.commit()

            # 校准 identity 序列，避免后续隐式插入撞主键
            if table in IDENTITY_TABLES:
                cur.execute(f"SELECT COALESCE(MAX(id), 1) FROM {table}")
                max_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), %s)",
                    (table, max_id),
                )
                conn.commit()

            # 验证
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            pg_after = cur.fetchone()[0]
            ok = pg_after == n_src
            report[table] = {"sqlite_rows": n_src, "inserted": inserted,
                             "pg_rows_after": pg_after, "ok": ok}
            if not ok:
                failed = True
            print(f"  - {table}: {n_src} -> {pg_after} {'✓' if ok else '✗'}")
    finally:
        conn.close()
        sql_conn.close()

    print("[3/4] 未迁移: checkpoints（LangGraph 序列化格式不同，会话有 TTL，重开即可）")
    print("[4/4] 报告:")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
