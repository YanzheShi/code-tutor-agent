"""清空所有数据并重置自增 ID，把数据库恢复到初始状态。

覆盖当前 schema 下的全部用户表（题目 / 提交 / 画像 V1+V2+记忆 / 编辑轨迹 /
轨迹分析 / 会话状态 / Token 用量等），**表名单由 sqlite_master 动态发现**，
新增表时无需改脚本即可自动纳入清空范围。

操作前自动备份主库与 checkpoints 库（含 -wal / -shm），并提示重启后端。

用法：
    uv run python scripts/reset_all_data.py                # 交互确认后清空
    uv run python scripts/reset_all_data.py --yes           # 跳过交互确认
    uv run python scripts/reset_all_data.py --dry-run       # 仅预览，不删任何数据
    uv run python scripts/reset_all_data.py --keep-profiles # 保留画像(V1/V2/记忆)，只清业务数据
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time


# ── 路径解析 ──

def _resolve_db_path() -> str:
    """主库路径：优先读业务代码里的 DB_PATH，失败回退到约定位置。"""
    try:
        from code_tutor_agent.db.database import DB_PATH
        return os.path.abspath(DB_PATH)
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "data", "db", "code_tutor.db")


def _resolve_checkpoints_dir() -> str:
    """LangGraph checkpoints 库所在目录。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "checkpoints")


# ── 通用工具 ──

def _user_tables(db_path: str) -> list[tuple[str, bool]]:
    """返回 (表名, 是否 AUTOINCREMENT) 列表，排除 sqlite 内部表。"""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        con.close()
    return [(name, bool(sql and "AUTOINCREMENT" in sql)) for name, sql in rows]


def _count(db_path: str, table: str) -> int:
    try:
        con = sqlite3.connect(db_path)
        try:
            return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            con.close()
    except sqlite3.OperationalError:
        return 0


def _backup(db_path: str) -> tuple[str, list[str]]:
    """复制主库及其 -wal / -shm 到带时间戳的 .bak_<ts> 文件，返回 (时间戳, 备份路径列表)。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    backed: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            dst = src + f".bak_{ts}"
            shutil.copy2(src, dst)
            backed.append(dst)
    return ts, backed


def _reset_db(db_path: str, keep: tuple[str, ...] = ()) -> dict[str, int]:
    """清空所有用户表并重置对应自增计数；返回被清空的表名 → 删除行数。

    Args:
        keep: 需要保留（不清除）的表名元组，例如 ("profiles",)。
    """
    cleared: dict[str, int] = {}
    tables = _user_tables(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        for name, is_ai in tables:
            if name in keep:
                continue
            deleted = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            con.execute(f'DELETE FROM "{name}"')
            if is_ai:
                # SQLite 的 AUTOINCREMENT 计数器存在 sqlite_sequence，需一并清零
                con.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
            cleared[name] = deleted
        con.commit()
    finally:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        con.close()
    return cleared


# ── 主流程 ──

def main() -> None:
    parser = argparse.ArgumentParser(description="清空所有数据并重置自增 ID")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不执行任何删除")
    parser.add_argument(
        "--keep-profiles",
        action="store_true",
        help="保留画像表(profiles：V1/V2/记忆)，只清业务数据",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path()
    cp_dir = _resolve_checkpoints_dir()
    cp_db = os.path.join(cp_dir, "checkpoints.sqlite")

    if not os.path.exists(db_path):
        print(f"未找到主数据库文件：{db_path}")
        return

    keep = ("profiles",) if args.keep_profiles else ()

    # ── 统计（清空前）──
    main_tables = _user_tables(db_path)
    before = {name: _count(db_path, name) for name, _ in main_tables if name not in keep}

    cp_before: dict[str, int] = {}
    if os.path.exists(cp_db):
        cp_before = {name: _count(cp_db, name) for name, _ in _user_tables(cp_db)}

    print(f"主库：{db_path}")
    for name, cnt in before.items():
        print(f"  {name}: {cnt}")
    if cp_before:
        print(f"checkpoints 库：{cp_db}")
        for name, cnt in cp_before.items():
            print(f"  {name}: {cnt}")
    if keep:
        print(f"保留（不清除）：{', '.join(keep)}")

    if args.dry_run:
        print("\n[dry-run] 仅预览，未做任何修改。")
        return

    if not args.yes:
        ans = input("\n确认清空以上所有数据并重置 ID？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    # ── 备份（主库 + checkpoints 库）──
    ts, backed = _backup(db_path)
    print(f"\n已备份主库（.bak_{ts}，共 {len(backed)} 个文件）")
    cp_backed = 0
    if os.path.exists(cp_db):
        _, cp_files = _backup(cp_db)
        cp_backed = len(cp_files)
        print(f"已备份 checkpoints 库（共 {cp_backed} 个文件）")

    # ── 清空主库 ──
    cleared = _reset_db(db_path, keep=keep)
    print(f"\n主库已清空 {len(cleared)} 张表，删除行数：")
    for name, n in cleared.items():
        print(f"  {name}: {n}")

    # ── 清空 checkpoints 库 ──
    if os.path.exists(cp_db):
        cp_cleared = _reset_db(cp_db)
        print(f"checkpoints 库已清空 {len(cp_cleared)} 张表：")
        for name, n in cp_cleared.items():
            print(f"  {name}: {n}")

    # ── 验证 ──
    after = {name: _count(db_path, name) for name in before}
    residual = {name: n for name, n in after.items() if n}
    print("\n校验（应为全 0）：")
    for name, n in after.items():
        print(f"  {name}: {n}")
    if residual:
        print(f"⚠️ 仍有残留数据：{residual}")
    else:
        print("✓ 主库数据已全部清空，ID 已重置。")

    print("\n注意：需要重启后端服务才能完全生效（checkpointer 持有旧连接）。")


if __name__ == "__main__":
    main()
