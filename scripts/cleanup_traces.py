"""清理过期的轨迹分析数据，供系统定时任务（cron / Windows 任务计划程序）调用。

只清理「轨迹分析派生数据」5 张表：edit_traces / trace_threads / trace_analysis /
analysis_results / trace_summaries，绝不碰 submissions / profiles / problems 等核心
业务表。清理规则与表清单由 `database.purge_trace_data` 单点维护，本脚本只负责
解析参数、调用与结果输出。

用法：
    uv run python scripts/cleanup_traces.py                 # 清理 30 天前（默认）
    uv run python scripts/cleanup_traces.py --days 7        # 只保留最近 7 天
    uv run python scripts/cleanup_traces.py --dry-run       # 仅统计待删行数，不删
    uv run python scripts/cleanup_traces.py --json          # JSON 输出，便于日志采集
    uv run python scripts/cleanup_traces.py --quiet         # 无可清理数据时静默

退出码（供任务计划程序判断成败）：
    0  成功（含「无数据可清理」）
    1  清理失败（purge_trace_data 未返回任何统计，详情见日志）
    2  参数非法 / 数据库不存在 / 导入失败
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


# Windows 控制台与任务计划程序下 stdout 常为 GBK，避免中文输出触发 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非 UTF-8 环境下的兜底
    pass


# ── 路径解析 ──

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")

# 允许 `python scripts/cleanup_traces.py` 直接运行（项目未以 editable 安装时也能 import）
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _resolve_db_path() -> str:
    """主库路径：优先读业务代码里的 DB_PATH，失败回退到约定位置。"""
    try:
        from code_tutor_agent.db.database import DB_PATH
        return os.path.abspath(DB_PATH)
    except Exception:
        return os.path.join(_ROOT, "data", "db", "code_tutor.db")


def main() -> int:
    parser = argparse.ArgumentParser(description="清理过期的轨迹分析数据")
    parser.add_argument(
        "--days", type=int, default=30,
        help="保留天数：早于 (now - days) 的行会被清理，默认 30",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅统计待删行数，不执行删除")
    parser.add_argument("--json", action="store_true", help="以 JSON 单行输出结果，便于日志采集")
    parser.add_argument("--quiet", action="store_true", help="无待清理数据时静默（退出码仍为 0）")
    args = parser.parse_args()

    # 让 purge_trace_data 内部的 INFO 日志可见，便于定时任务留痕排查
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.days < 1:
        print("--days 必须 >= 1", file=sys.stderr)
        return 2

    db_path = _resolve_db_path()
    if not os.path.exists(db_path):
        print(f"未找到数据库文件：{db_path}", file=sys.stderr)
        return 2

    try:
        from code_tutor_agent.db.database import purge_trace_data
    except Exception as exc:
        print(f"导入 code_tutor_agent.db.database 失败：{exc}", file=sys.stderr)
        return 2

    # 删除与预演走同一函数，表清单/列名只有一处定义，不会漂移
    stats = purge_trace_data(days=args.days, dry_run=args.dry_run)

    if not stats:
        # purge_trace_data 内部 try/except 吞异常：连第一张表都没统计到即视为失败
        print("purge_trace_data 未返回任何统计，可能执行失败（详见上方日志）", file=sys.stderr)
        return 1

    total = sum(stats.values())
    result_key = "pending" if args.dry_run else "deleted"

    if args.json:
        print(json.dumps(
            {"days": args.days, "dry_run": args.dry_run, "db": db_path,
             result_key: stats, "total": total},
            ensure_ascii=False,
        ))
        return 0

    if total or not args.quiet:
        mode = "待清理（dry-run，未删除）" if args.dry_run else "已清理"
        print(f"{mode} 早于 {args.days} 天的轨迹数据")
        print(f"数据库：{db_path}")
        for table, n in stats.items():
            print(f"  {table}: {n}")
        print(f"合计：{total}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
