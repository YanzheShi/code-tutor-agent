"""清空数据库中的题目（problems）及其提交记录（submissions）。

用于开发/演示阶段把题库整体清空后重新出题。操作前会自动对
``data/db/code_tutor.db`` 做带时间戳的备份（含 -wal/-shm）。

用法：
    uv run python scripts/clear_problems.py            # 交互确认后清空
    uv run python scripts/clear_problems.py --yes      # 跳过确认（CI / 脚本调用）
    uv run python scripts/clear_problems.py --dry-run  # 仅预览受影响行数，不删
    uv run python scripts/clear_problems.py --no-backup # 不备份（慎用）

注意：SQLite 默认外键约束关闭，DELETE 两张表无级联风险；
备份文件名形如 ``code_tutor.db.bak_YYYYMMDD_HHMMSS``。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time


def _resolve_db_path() -> str:
    """优先复用项目内 database 模块声明的 DB_PATH，失败则按相对路径推算。"""
    try:
        from code_tutor_agent.db.database import DB_PATH

        return DB_PATH
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "data", "db", "code_tutor.db")


def _backup(db_path: str) -> str:
    """对 db 文件及其 -wal/-shm 做带时间戳备份，返回时间戳后缀。"""
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
    parser = argparse.ArgumentParser(description="清空数据库中的题目与提交记录")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认直接清空")
    parser.add_argument("--dry-run", action="store_true", help="仅预览受影响行数，不执行删除")
    parser.add_argument("--no-backup", action="store_true", help="不创建备份（慎用）")
    args = parser.parse_args()

    db_path = _resolve_db_path()
    if not os.path.exists(db_path):
        print(f"未找到数据库文件：{db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()
    before_problems = _count(cur, "problems")
    before_submissions = _count(cur, "submissions")
    conn.close()

    print(f"数据库：{db_path}")
    print(f"  当前 problems={before_problems}，submissions={before_submissions}")

    if args.dry_run:
        print("[dry-run] 未做任何修改。")
        return

    if not args.yes:
        ans = input("确认清空以上所有题目与提交记录？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    if not args.no_backup:
        ts = _backup(db_path)
        print(f"已备份：.bak_{ts}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()
    cur.execute("DELETE FROM problems")
    cur.execute("DELETE FROM submissions")
    conn.commit()
    after_problems = _count(cur, "problems")
    after_submissions = _count(cur, "submissions")
    conn.close()

    print(f"完成：problems {before_problems} -> {after_problems}，"
          f"submissions {before_submissions} -> {after_submissions}")


if __name__ == "__main__":
    main()
