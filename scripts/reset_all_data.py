"""清空所有数据：题目、提交记录、个人画像、活跃记录、会话状态，并重置 ID。

用于开发/演示阶段把数据库完全重置到初始状态。操作前自动备份。

用法：
    uv run python scripts/reset_all_data.py                # 交互确认后清空
    uv run python scripts/reset_all_data.py --yes           # 跳过确认
    uv run python scripts/reset_all_data.py --dry-run       # 仅预览，不删
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time


def _resolve_db_path() -> str:
    try:
        from code_tutor_agent.db.database import DB_PATH
        return DB_PATH
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "data", "db", "code_tutor.db")


def _resolve_checkpoints_dir() -> str:
    """checkpoints 数据库目录（checkpoints.db + checkpoints.sqlite）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "checkpoints")


def _backup(db_path: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, src + f".bak_{ts}")
    return ts


def _count(cursor: sqlite3.Cursor, table: str) -> int:
    try:
        return cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="清空所有数据并重置 ID")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不执行")
    args = parser.parse_args()

    db_path = _resolve_db_path()
    checkpoints_dir = _resolve_checkpoints_dir()
    checkpoints_db = os.path.join(checkpoints_dir, "checkpoints.sqlite")

    db_paths = [db_path]
    if os.path.exists(checkpoints_db):
        db_paths.append(checkpoints_db)

    if not any(os.path.exists(p) for p in db_paths):
        print("未找到数据库文件。")
        return

    # ── 统计 ──
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    before = {
        "problems": _count(cur, "problems"),
        "submissions": _count(cur, "submissions"),
        "profiles": _count(cur, "profiles"),
        "session_activity": _count(cur, "session_activity"),
    }
    conn.close()

    # 统计 checkpoints
    cp_before = 0
    if os.path.exists(checkpoints_db):
        try:
            conn2 = sqlite3.connect(checkpoints_db)
            cp_before = conn2.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            conn2.close()
        except Exception:
            cp_before = -1

    print(f"数据库：{db_path}")
    for k, v in before.items():
        print(f"  {k}: {v}")
    print(f"  checkpoints: {cp_before}")

    if args.dry_run:
        print("[dry-run] 未做任何修改。")
        return

    if not args.yes:
        ans = input("确认清空以上所有数据并重置 ID？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    # ── 备份 ──
    ts = _backup(db_path)
    print(f"已备份 code_tutor.db：.bak_{ts}")

    # ── 清空数据表 ──
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    cur.execute("DELETE FROM problems")
    cur.execute("DELETE FROM submissions")
    cur.execute("DELETE FROM profiles")
    cur.execute("DELETE FROM session_activity")

    # ── 重置自增 ID ──
    # SQLite 的 AUTOINCREMENT 计数器存储在 sqlite_sequence 表中
    cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('problems', 'submissions')")

    conn.commit()
    conn.close()

    # ── 清空 checkpoints ──
    if os.path.exists(checkpoints_db):
        try:
            conn2 = sqlite3.connect(checkpoints_db)
            # 查询实际存在的表，避免 LangGraph 版本差异导致表名不匹配
            tables = {r[0] for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for tbl in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "writes"):
                if tbl in tables:
                    conn2.execute(f"DELETE FROM {tbl}")
            conn2.commit()
            conn2.close()
            print("已清空 checkpoints 数据库")
        except Exception as exc:
            print(f"清空 checkpoints 失败（非致命）: {exc}")

    print("完成：所有数据已清空，ID 已重置。")
    print("注意：需要重启后端服务才能生效（checkpointer 持有旧连接）。")


if __name__ == "__main__":
    main()