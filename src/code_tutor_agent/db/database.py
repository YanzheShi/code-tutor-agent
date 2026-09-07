"""PostgreSQL-backed persistent problem storage (2026-09-07 迁移自 SQLite).

存储层已整体迁移到 PostgreSQL（连接池 + psycopg3，见 ``db/pg_compat.py``）：
SQL 语句保持 sqlite3 方言写法（`?` 占位符等）由兼容层统一翻译，业务 DAO 无感。
时间列语义不变：**本地时间**（原 ``LOCALTIMESTAMP`` → ``LOCALTIMESTAMP``）。

存量数据迁移：scripts/migrate_sqlite_to_pg.py（SQLite → PG 一次性 ETL）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Optional

from .models import DBProblem, DBSubmission
from .pg_compat import (
    IntegrityError,
    OperationalError,
    get_conn,
    ping as pg_ping,
)

# 旧名兼容：_with_conn 及部分调用点仍用 _get_conn（现由 pg_compat 连接池提供）
_get_conn = get_conn

logger = logging.getLogger(__name__)


# 旧 SQLite 库文件路径（保留：作为存量数据 ETL 的默认源路径；运行时不再读写）。
DB_PATH = os.environ.get(
    "CTA_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "db", "code_tutor.db"),
)


# 长事务会独占写锁，是 "database is locked" 的头号嫌疑（判题链路上锁过久会让
# 其他线程排队到 busy_timeout 耗尽）。超过阈值就打栈，便于定位是谁在事务里做慢活。
_SLOW_TXN_SEC = 2.0


def _frame_line(frame, limit: int = 5) -> str:
    """把调用栈压成单行（多行栈会被日志管线截断，实测只剩第一行，定位不到人）。"""
    frames = traceback.extract_stack(frame)[-limit:]
    return " << ".join(f"{f.name}@{os.path.basename(f.filename)}:{f.lineno}" for f in frames)


def _all_thread_stacks() -> str:
    """快照全部线程栈——被阻塞的是本线程，持锁的真凶往往在别的线程里。"""
    parts = []
    for t in threading.enumerate():
        frame = sys._current_frames().get(t.ident)
        if frame is None:
            continue
        parts.append(f"[{t.name}] {_frame_line(frame, limit=4)}")
    return " // ".join(parts)


def _m_record(name: str, value: float = 1.0) -> None:
    """监控埋点（非侵入：失败静默，绝不影响 DB 主流程）。"""
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        get_registry().record(name, value)
    except Exception:
        pass


def _with_conn(fn):
    """Execute *fn(cursor)* inside a try/commit/except/rollback/finally block.

    Ensures the connection is always closed, even on error.
    """
    conn = _get_conn()
    started = time.perf_counter()
    try:
        result = fn(conn.cursor())
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        try:
            if "database is locked" in str(exc).lower():
                _m_record("db_locked")
        except Exception:
            pass
        raise
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= _SLOW_TXN_SEC:
            _m_record("db_slow_tx")
            frame = sys._current_frames().get(threading.current_thread().ident)
            logger.warning(
                "slow DB transaction %.2fs | self: %s | threads: %s",
                elapsed,
                _frame_line(frame) if frame else "n/a",
                _all_thread_stacks(),
            )
        conn.close()


def init_db() -> None:
    """Create tables if they do not exist yet (D2 schema)."""
    logger.info("▶ init_db()")
    try:
        _with_conn(lambda cursor: _init_db_tables(cursor))
        backfill_submissions_user_id()
    except Exception as exc:
        logger.error("init_db() failed: %s", exc)
        raise


def backfill_submissions_user_id() -> int:
    """存量提交归属回填（幂等，2026-09-06）。

    背景：save_submission 曾漏传 user_id，历史提交全部记在 'default' 名下，
    而查询端按真实用户 ID 过滤 → 个人中心「我的提交」一直为空。
    修复落库后，此处把「user_id='default' 但会话归属已知」的存量行回填成
    会话真实归属；会话无归属记录（遗留数据）保持不动，绝不臆测归属。
    每次启动幂等执行：回填过的行不再满足 WHERE 条件。
    """
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE submissions SET user_id = ("
                "  SELECT sa.user_id FROM session_activity sa"
                "  WHERE sa.session_id = submissions.session_id"
                "    AND sa.user_id != 'default'"
                ") "
                "WHERE user_id = 'default' "
                "  AND session_id != '' "
                "  AND EXISTS ("
                "    SELECT 1 FROM session_activity sa2"
                "    WHERE sa2.session_id = submissions.session_id"
                "      AND sa2.user_id != 'default'"
                ")",
            )
            return cursor.rowcount
        n = _with_conn(_do)
        if n:
            logger.info("backfill_submissions_user_id(): %d legacy rows re-owned", n)
        return n
    except Exception as exc:
        # 非致命：回填失败不影响启动（表不存在/锁竞争等）
        logger.warning("backfill_submissions_user_id() failed (non-fatal): %s", exc)
        return 0


def _pg_table_exists(cursor, table: str) -> bool:
    """search_path 感知的表存在性检查：to_regclass 模拟 PG 对未限定表名的
    解析规则（按 search_path 顺序取第一个命中），与后续 SQL 的实际落点一致。"""
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return cursor.fetchone()[0] is True


def _pg_column_exists(cursor, table: str, column: str) -> bool:
    """search_path 感知的列存在性检查。

    必须先解析出 search_path 下 table 实际落在哪个 schema，再查该 schema 的列。
    不能用 `table_schema = ANY (current_schemas(true))` 一刀切：同库多 schema
    （如测试 schema 与装着真数据的 public 共存）时，目标列在本 schema 缺失、
    却在 public 存在，宽过滤会误判「列已存在」而跳过本 schema 的补列 ALTER
    （实测：code_tutor 库跑测试 schema 时 starter_code_norm 补列被跳过）。
    """
    cursor.execute(
        "SELECT n.nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s)",
        (table,),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    schema = row[0]
    cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s LIMIT 1",
        (schema, table, column),
    )
    return cursor.fetchone() is not None


def _init_db_tables(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS problems (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            title TEXT NOT NULL UNIQUE,
            topic TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            description TEXT NOT NULL,
            test_cases_json TEXT NOT NULL,
            optimal_solution TEXT NOT NULL DEFAULT '',
            brute_solution TEXT DEFAULT '',
            function_signature TEXT NOT NULL DEFAULT '',
            adversarial_spec_json TEXT DEFAULT '',
            time_complexity TEXT DEFAULT '',
            space_complexity TEXT DEFAULT '',
            novelty_score REAL DEFAULT 7.0,
            starter_code TEXT NOT NULL DEFAULT '',
            visible_test_cases_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'generated',
            source_url TEXT DEFAULT '',
            alternative_solutions TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # 幂等补列：PG 事务内失败的 ALTER 会终止整个事务（InFailedSqlTransaction），
    # 不能像 SQLite 那样靠「语句级失败继续」——改为 information_schema 预检查，
    # 只对缺失的列执行 ALTER。
    for table, col, col_type, default in [
        ("problems", "starter_code", "TEXT", "''"),
        ("problems", "starter_code_norm", "TEXT", "''"),
        ("problems", "visible_test_cases_json", "TEXT NOT NULL", "'[]'"),
        ("problems", "source", "TEXT NOT NULL", "'generated'"),
        ("problems", "source_url", "TEXT", "''"),
        ("problems", "alternative_solutions", "TEXT NOT NULL", "'[]'"),
        ("problems", "function_signature", "TEXT", "''"),
        ("problems", "constraints_json", "TEXT NOT NULL", "'[]'"),
        ("submissions", "verdict", "TEXT", "''"),
        ("submissions", "judge_results", "TEXT", "'[]'"),
        ("submissions", "session_id", "TEXT", "''"),
        ("submissions", "user_id", "TEXT", "'default'"),
        ("edit_traces", "problem_id", "TEXT NOT NULL", "'default'"),
        ("session_activity", "user_id", "TEXT", "'default'"),
    ]:
        # 缺表跳过（表在函数后文才建）；缺列才 ALTER（SQLite 语义下的幂等补列）
        if _pg_table_exists(cursor, table) and not _pg_column_exists(cursor, table, col):
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN {col} {col_type} DEFAULT {default}"
            )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 用户表（邮箱密码登录，多用户支持）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 用户设置表（自定义 LLM 接入：API key / base URL / model，per-user）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            llm_mode TEXT NOT NULL DEFAULT 'default',
            llm_model TEXT NOT NULL DEFAULT '',
            llm_base_url TEXT NOT NULL DEFAULT '',
            llm_api_key TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 邀请码表（注册准入：额度 + 有效期，admin 面板生成/停用）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 密码重置码（忘记密码自助流程， Brevo 可选；存哈希不存明文）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_codes (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            email TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 会话活跃时间表（TTL 自动清理用；user_id 记录归属，越权校验的事实源）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_activity (
            session_id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            last_active_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 编辑轨迹（前端实时采集，按 (session_id, problem_id) 联合主键隔离）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS edit_traces (
            session_id TEXT NOT NULL,
            user_id TEXT DEFAULT 'default',
            problem_id TEXT NOT NULL DEFAULT 'default',
            events_json TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            PRIMARY KEY (session_id, problem_id)
        )
    """)

    # 迁移：旧 schema（session_id 单主键）→ 新 schema（联合主键）
    # 将旧行的事件按内嵌 problem_id 拆分到各行，并去除事件级冗余 problem_id
    cursor.execute(
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
        "AND tc.table_schema = kcu.table_schema "
        "WHERE tc.table_name = 'edit_traces' AND tc.constraint_type = 'PRIMARY KEY'"
    )
    _et_pk = [r[0] for r in cursor.fetchall()]
    if len(_et_pk) == 1:
        cursor.execute("ALTER TABLE edit_traces RENAME TO _edit_traces_old")
        cursor.execute("""
            CREATE TABLE edit_traces (
                session_id TEXT NOT NULL,
                user_id TEXT DEFAULT 'default',
                problem_id TEXT NOT NULL DEFAULT 'default',
                events_json TEXT NOT NULL DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
                PRIMARY KEY (session_id, problem_id)
            )
        """)
        for _et_row in cursor.execute(
            "SELECT session_id, user_id, problem_id, events_json FROM _edit_traces_old"
        ).fetchall():
            _et_events = json.loads(_et_row["events_json"] or "[]")
            _et_groups: dict[str, list[dict]] = {}
            for _ev in _et_events:
                if not isinstance(_ev, dict):
                    continue
                _epid = str(_ev.get("problem_id") or _et_row["problem_id"] or "default")
                _ev_clean = {k: v for k, v in _ev.items() if k != "problem_id"}
                _et_groups.setdefault(_epid, []).append(_ev_clean)
            if not _et_groups:
                _et_groups[str(_et_row["problem_id"] or "default")] = []
            for _epid, _evs in _et_groups.items():
                cursor.execute(
                    "INSERT INTO edit_traces (session_id, user_id, problem_id, events_json) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (session_id, problem_id) DO UPDATE SET "
                    "user_id = excluded.user_id, events_json = excluded.events_json",
                    (_et_row["session_id"], _et_row["user_id"], _epid,
                     json.dumps(_evs, ensure_ascii=False))
                )
        cursor.execute("DROP TABLE _edit_traces_old")
        logger.info("edit_traces migrated to composite PK (session_id, problem_id)")

    # ── 独立轨迹分析结论（纯展示，不回灌画像；按 session 缓存）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trace_analysis (
            session_id TEXT PRIMARY KEY,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP
        )
    """)

    # ── 轨迹分析（按题隔离，多轮线程首轮结构化结论；不回灌画像）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS analysis_results (
            session_id TEXT NOT NULL,
            problem_id TEXT NOT NULL DEFAULT 'default',
            result_json TEXT NOT NULL DEFAULT '{}',
            model TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            PRIMARY KEY (session_id, problem_id)
        )
    """)

    # ── 轨迹分析过渡摘要（双落点：可见卡 + 历史回看；按 pid 覆盖取最新）──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trace_summaries (
            session_id TEXT NOT NULL,
            problem_id TEXT NOT NULL DEFAULT 'default',
            transition_action TEXT DEFAULT '',
            summary_json TEXT NOT NULL DEFAULT '{}',
            token_est INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            PRIMARY KEY (session_id, problem_id)
        )
    """)

    # ── 轨迹分析多轮线程（按题隔离，持久化消息历史；不回灌画像）──
    # 替代进程内 dict：服务重启后多轮追问与过渡压缩仍可读回线程 transcript。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trace_threads (
            session_id TEXT NOT NULL,
            problem_id TEXT NOT NULL DEFAULT 'default',
            messages_json TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            PRIMARY KEY (session_id, problem_id)
        )
    """)

    try:
        cursor.execute("ALTER TABLE problems DROP COLUMN IF EXISTS solution")
    except Exception:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            problem_id INTEGER NOT NULL,
            session_id TEXT DEFAULT '',
            user_id TEXT DEFAULT 'default',
            student_code TEXT NOT NULL,
            status TEXT NOT NULL,
            verdict TEXT DEFAULT '',
            judge_results TEXT DEFAULT '[]',
            feedback TEXT,
            created_at TIMESTAMP DEFAULT LOCALTIMESTAMP,
            FOREIGN KEY (problem_id) REFERENCES problems (id)
        )
    """)

    # ── Token 用量明细(成本计量,见 docs/token-cost-control-design.md)──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            ts TIMESTAMP DEFAULT LOCALTIMESTAMP,
            session_id TEXT DEFAULT '',
            user_id TEXT DEFAULT 'default',
            purpose TEXT NOT NULL,
            model_alias TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            mode TEXT DEFAULT '',
            topic TEXT DEFAULT '',
            difficulty TEXT DEFAULT '',
            problem_id INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            latency_ms INTEGER DEFAULT 0,
            run_id TEXT DEFAULT ''
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(ts)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_purpose ON token_usage(purpose)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_session ON token_usage(session_id)")

    # ── Token 用量按 日期×purpose×model×user 预聚合(报表免全表扫)──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_usage_daily (
            day TEXT NOT NULL,
            purpose TEXT NOT NULL,
            model_alias TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default',
            call_count INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            PRIMARY KEY (day, purpose, model_alias, user_id)
        )
    """)
    # 监控告警历史（docs/monitoring-alerts-design.md §8）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id           INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            rule_id      TEXT NOT NULL,
            severity     TEXT NOT NULL,
            status       TEXT NOT NULL,
            title        TEXT NOT NULL,
            detail       TEXT,
            triggered_at TEXT NOT NULL,
            notified     INTEGER DEFAULT 0
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_history_time "
        "ON alert_history(triggered_at)"
    )
    # 主页公告横幅（docs/monitoring-alerts-design.md §15，P1 双来源）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            level      TEXT NOT NULL,
            title      TEXT NOT NULL,
            content    TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT 'admin',
            rule_id    TEXT,
            active     INTEGER DEFAULT 1,
            starts_at  TEXT,
            ends_at    TEXT,
            created_at TEXT NOT NULL
        )
    """)


# ── helpers ──


def _row_to_db_problem(row) -> DBProblem:
    """Convert a result Row (from SELECT * on problems) to DBProblem."""
    data = dict(row)
    return DBProblem(**data)


def _row_to_db_submission(row) -> DBSubmission:
    """Convert a result Row (from SELECT on submissions) to DBSubmission.

    注意（2026-09-07 修复存量 bug）：旧实现把 created_at pop 成 timestamp 再传入
    模型，但 DBSubmission 并无 timestamp 字段（只有同名只读 property），extra 被
    pydantic 静默忽略 → created_at 恒为空串、提交记录时间戳一直显示空。
    现保留 created_at 原字段，timestamp 由 property 代理返回，两处语义同时成立。
    """
    return DBSubmission(**dict(row))


# ── starter_code 归一化（去重用，纯确定性、无 LLM）──
#
# LLM 每次生成的 starter_code 会有微妙但非本质的差异：
#   - 类型注解 List[int] vs list[int]
#   - typing import（from typing import ...）的有无
#   - 结构体定义（ListNode / TreeNode / Node 等）由运行时 prologue 注入，非题目本质
#   - 占位符 ... / pass / 空行
#   - 全半角、空白、注释前缀（# Definition for singly-linked list.）
# 归一化后比对即可把「同一道题的不同形态」判为重复，复用旧 id。

_TYPING_ALIASES = {
    "List": "list", "Dict": "dict", "Set": "set", "Tuple": "tuple",
    "FrozenSet": "frozenset", "Deque": "deque",
}
_STRUCT_CLASSES = ("ListNode", "TreeNode", "Node", "GraphNode")


def normalize_starter_code(code: str) -> str:
    """对 starter_code 做确定性归一化，用于去重比对。

    步骤：
      1. 剥 ``from typing import ...`` 整行（运行时由 typing 提供，非本质）。
      2. 剥结构体定义块（``# Definition for ...`` 注释 + ``class ListNode:`` 及其
         方法体），这些是运行时 prologue 注入的。
      3. typing 别名归一：``List[`` → ``list[`` 等。
      4. ``Optional[X]`` → ``X | None``。
      5. 占位符归一：``pass`` / ``...`` / 空语句 → 统一空。
      6. 空白归一：CRLF→LF、折叠连续空白、去行尾空白、strip。

    Returns:
        归一化后的字符串；输入为空返回空串。
    """
    if not code:
        return ""

    lines = code.replace("\r\n", "\n").split("\n")

    cleaned: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        # 跳过 typing import 行（完整行或行内 from ... import）
        if stripped.startswith("from typing import"):
            i += 1
            continue
        # 跳过结构体定义注释前缀
        if stripped.startswith("# Definition for") or stripped.startswith("# ====="):
            i += 1
            continue
        # 检测结构体 class 定义块：class ListNode / TreeNode / Node / GraphNode
        m = re.match(r"^(\s*)class\s+(" + "|".join(_STRUCT_CLASSES) + r")\b", raw)
        if m:
            # 吞掉整个 class 定义块（直到缩进回到该 class 声明级别或文件结束）
            base_indent = len(raw) - len(raw.lstrip())
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    i += 1
                    continue
                ind = len(nxt) - len(nxt.lstrip())
                # 缩进比 class 声明浅 → 块结束
                if ind <= base_indent:
                    break
                i += 1
            continue
        cleaned.append(raw)
        i += 1

    text = "\n".join(cleaned)

    # typing 别名归一（含泛型参数）
    for alias, repl in _TYPING_ALIASES.items():
        text = re.sub(rf"\b{alias}\[", repl + "[", text)

    # Optional[X] → X | None
    def _opt_repl(mo):
        return f"{mo.group(1)} | None"
    text = re.sub(r"Optional\[(.*?)\]", _opt_repl, text)

    # 占位符归一：独立的 pass / ... 行 → 空
    text = re.sub(r"^\s*(pass|\.\.\.)\s*$", "", text, flags=re.MULTILINE)

    # 空白归一
    text = text.replace("\t", " ")
    text = re.sub(r"[ \t]+", " ", text)        # 折叠行内连续空白
    text = re.sub(r" ?\n ?", "\n", text)        # 去行首尾空格
    text = re.sub(r"\n{2,}", "\n", text)        # 折叠空行
    text = text.strip()
    return text


def save_problem(problem_dict: dict) -> tuple[int, bool]:
    """Save a problem to the database.

    去重策略（2026-08-20 改造）：按 ``starter_code`` 的**归一化**形态去重——
    归一化后与库中已有题目命中即视为同一道题，直接复用旧 id，
    **不插入新行、不触发测试用例生成**。

    Args:
        problem_dict: Dict with keys matching the logical problem schema
            (title, topic, difficulty, description, test_cases, etc.).
            Accepts both camelCase (test_cases) and snake_case (test_cases_json) keys.

    Returns:
        ``(problem_id, reused)`` 二元组：
        - ``problem_id``：复用的旧 id 或新插入的 id。
        - ``reused``：True 表示命中归一化去重、复用了已有题目（调用方据此跳过
          测试用例后台生成）。
    """
    logger.info("▶ save_problem()")
    init_db()
    try:
        return _with_conn(lambda cursor: _save_problem(cursor, problem_dict))
    except Exception as exc:
        logger.error("save_problem() failed for '%s': %s", problem_dict.get("title", "?"), exc)
        raise


def _save_problem(cursor, problem_dict: dict) -> tuple[int, bool]:
    title = problem_dict.get("title", "")
    if not title:
        raise ValueError("save_problem() requires a 'title'")

    # ── 归一化去重：starter_code 形态一致即复用旧 id ──
    norm = normalize_starter_code(problem_dict.get("starter_code", ""))
    if norm:
        cursor.execute(
            "SELECT id, starter_code_norm FROM problems WHERE starter_code_norm = ?",
            (norm,),
        )
        hit = cursor.fetchone()
        if hit:
            logger.info(
                "Problem '%s' dedup by normalized starter_code — reusing id=%d",
                title, hit["id"],
            )
            return hit["id"], True

    # ── source_url 精确去重（LeetCode 通道补强）──
    # LeetCode 题 starter_code 归一化后基本恒等，但 prologue 注入顺序差异可能漏判；
    # 同 slug 的 source_url 必然一致，用 url 精确兜底复用，彻底堵死重复落库。
    src_url = (problem_dict.get("source_url") or "").strip()
    if src_url:
        cursor.execute(
            "SELECT id FROM problems WHERE source_url = ?",
            (src_url,),
        )
        url_hit = cursor.fetchone()
        if url_hit:
            logger.info(
                "Problem '%s' dedup by source_url '%s' — reusing id=%d",
                title, src_url, url_hit["id"],
            )
            return url_hit["id"], True

    alt = problem_dict.get("alternative_solutions", [])
    if not isinstance(alt, str):
        alt = json.dumps(alt, ensure_ascii=False)

    # Serialise test cases to JSON if they're Python lists
    test_cases = problem_dict.get("test_cases", [])
    if not isinstance(test_cases, str):
        test_cases = json.dumps(test_cases, ensure_ascii=False)

    visible_tcs = problem_dict.get("visible_test_cases", problem_dict.get("test_cases", []))
    if not isinstance(visible_tcs, str):
        visible_tcs = json.dumps(visible_tcs, ensure_ascii=False)

    adv_spec = problem_dict.get("adversarial_spec")
    adv_spec_json = json.dumps(adv_spec, ensure_ascii=False) if adv_spec else ""

    constraints = problem_dict.get("constraints", [])
    if not isinstance(constraints, str):
        constraints = json.dumps(constraints, ensure_ascii=False)

    # PG 语义：语句失败会终止整个事务，兜底回查必须能继续——用 SAVEPOINT 包住
    # INSERT，撞 UNIQUE 后 ROLLBACK TO SAVEPOINT 即可在同一事务内继续回查
    # （SQLite 同样支持该语法，跨库可移植）。
    cursor.execute("SAVEPOINT _sp_save_problem")
    try:
        cursor.execute("""
            INSERT INTO problems
                (title, topic, difficulty, description, test_cases_json, visible_test_cases_json,
                 optimal_solution, brute_solution, function_signature, adversarial_spec_json,
                 time_complexity, space_complexity, novelty_score, starter_code, starter_code_norm,
                 source, source_url, alternative_solutions, constraints_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """, (
            title,
            problem_dict.get("topic", ""),
            problem_dict.get("difficulty", ""),
            problem_dict.get("description", ""),
            test_cases,
            visible_tcs,
            problem_dict.get("optimal_solution", ""),
            problem_dict.get("brute_solution", ""),
            problem_dict.get("function_signature", ""),
            adv_spec_json,
            problem_dict.get("time_complexity", ""),
            problem_dict.get("space_complexity", ""),
            problem_dict.get("novelty_score", 7.0),
            problem_dict.get("starter_code", ""),
            norm,
            problem_dict.get("source", "generated"),
            problem_dict.get("source_url", ""),
            alt,
            constraints,
        ))
    except IntegrityError:
        # 并发竞态兜底：上面的"先查后插"不是原子的，两个用户同时落同一道题时，
        # 双方查重都看不到对方未提交的行，后提交者会撞 title UNIQUE。
        # 撞车后回查，把竞态归一成"复用旧题"，与查询路径的去重语义保持一致。
        cursor.execute("ROLLBACK TO SAVEPOINT _sp_save_problem")
        cursor.execute("RELEASE SAVEPOINT _sp_save_problem")
        fallback_id: Optional[int] = None
        if src_url:
            cursor.execute(
                "SELECT id FROM problems WHERE source_url = ?",
                (src_url,),
            )
            row = cursor.fetchone()
            fallback_id = row["id"] if row else None
        if fallback_id is None:
            cursor.execute("SELECT id FROM problems WHERE title = ?", (title,))
            row = cursor.fetchone()
            fallback_id = row["id"] if row else None
        if fallback_id is None:
            raise
        logger.info(
            "Problem '%s' dedup by concurrent INSERT fallback — reusing id=%d",
            title, fallback_id,
        )
        return fallback_id, True
    problem_id = cursor.lastrowid
    logger.info("save_problem() — id=%d, title=%s, reused=False", problem_id, title)
    return problem_id, False


# ── read ──


def get_problem_by_id(problem_id: int) -> Optional[DBProblem]:
    """Retrieve a problem by its ID.

    Returns:
        DBProblem if found, None otherwise.
    """
    logger.info("▶ get_problem_by_id()")
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT * FROM problems WHERE id = ?", (problem_id,)
        ).fetchone())

        if not row:
            return None
        return _row_to_db_problem(row)
    except Exception as exc:
        logger.error("get_problem_by_id(%d) failed: %s", problem_id, exc)
        raise


def get_all_problem_ids() -> list[int]:
    """Return all problem IDs (for pool queries)."""
    try:
        return _with_conn(lambda cursor: [
            r["id"] for r in cursor.execute("SELECT id FROM problems ORDER BY id").fetchall()
        ])
    except Exception as exc:
        logger.error("get_all_problem_ids() failed: %s", exc)
        raise


def get_problems_by_ids(problem_ids: list[int]) -> list[DBProblem]:
    """Batch-fetch problems by IDs in a single query.

    Returns:
        List of DBProblem in the same order as input IDs.
    """
    if not problem_ids:
        return []

    placeholders = ",".join("?" * len(problem_ids))
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            f"SELECT * FROM problems WHERE id IN ({placeholders}) ORDER BY id",
            problem_ids,
        ).fetchall())

        result = [_row_to_db_problem(row) for row in rows]
        logger.info("get_problems_by_ids() — %d problems fetched", len(result))
        return result
    except Exception as exc:
        logger.error("get_problems_by_ids(%s) failed: %s", problem_ids, exc)
        raise


# ── update ──


def update_problem_test_cases(
    problem_id: int,
    test_cases: list[dict],
    visible_test_cases: "list[dict] | None" = None,
) -> None:
    """Update test cases for an existing problem (Day2 background generation).

    若传入 ``visible_test_cases``，则同步回写 ``visible_test_cases_json``；
    否则保持原行为只更新 ``test_cases_json``。

    注意：后台生成时会用参考解重新验证示例/可见用例并覆盖 original 的
    LLM 编造期望，因此这里必须允许回写 visible，否则前端 "运行" 仍用
    LLM 编错的可见用例（见 generation._generate_complex_tests）。
    """
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE problems SET test_cases_json = ? WHERE id = ?",
                (json.dumps(test_cases, ensure_ascii=False), problem_id),
            )
            if visible_test_cases is not None:
                cursor.execute(
                    "UPDATE problems SET visible_test_cases_json = ? WHERE id = ?",
                    (json.dumps(visible_test_cases, ensure_ascii=False), problem_id),
                )
        _with_conn(_do)
        logger.info("update_problem_test_cases() — id=%d, %d test cases", problem_id, len(test_cases))
    except Exception as exc:
        logger.error("update_problem_test_cases(%d) failed: %s", problem_id, exc)
        raise


def update_problem_optimal_solution(problem_id: int, code: str) -> None:
    """Update optimal_solution for a problem (e.g. after LLM generates it for LeetCode imports)."""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "UPDATE problems SET optimal_solution = ? WHERE id = ?",
            (code, problem_id),
        ))
        logger.info("update_problem_optimal_solution() — id=%d, %d chars", problem_id, len(code))
    except Exception as exc:
        logger.error("update_problem_optimal_solution(%d) failed: %s", problem_id, exc)
        raise


def save_submission(
    problem_id: int, code: str, verdict: str, judge_results: list[dict],
    session_id: str = "", user_id: str = "default",
) -> int:
    """Save a submission record to the database. Returns the submission ID."""
    try:
        def _do(cursor):
                    cursor.execute(
                        "INSERT INTO submissions (problem_id, session_id, user_id, student_code, status, verdict, judge_results) "
                        "VALUES (?, ?, ?, ?, 'judged', ?, ?) RETURNING id",
                        (problem_id, session_id, user_id, code, verdict, json.dumps(judge_results, ensure_ascii=False)),
                    )
                    return cursor.lastrowid
        sub_id = _with_conn(_do)
        logger.info("save_submission() — id=%d, problem=%d, session=%s, user=%s, verdict=%s",
                    sub_id, problem_id, session_id, user_id, verdict)
        return sub_id
    except Exception as exc:
        logger.error("Failed to save submission for problem %d: %s", problem_id, exc)
        raise


def get_submissions_by_problem(problem_id: int, limit: int = 50, user_id: str | None = None) -> list[dict]:
    """Return recent submissions for a problem.

    Returns list of dicts (via DBSubmission.to_dict()) for frontend compatibility.
    user_id 非空时按用户过滤（多用户隔离：只看自己的提交历史）。
    """
    try:
        sql = (
            "SELECT id, problem_id, session_id, student_code, verdict, judge_results, status, created_at "
            "FROM submissions WHERE problem_id = ?"
        )
        params: list = [problem_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = _with_conn(lambda cursor: cursor.execute(sql, params).fetchall())

        result = []
        for row in rows:
            sub = DBSubmission(**dict(row))
            result.append(sub.to_dict())
        logger.info("get_submissions_by_problem() — problem=%d, %d rows", problem_id, len(result))
        return result
    except Exception as exc:
        logger.error("get_submissions_by_problem(%d) failed: %s", problem_id, exc)
        raise


def get_submissions_by_session(session_id: str, limit: int = 50) -> list[dict]:
    """Return all submissions for a given session."""
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, problem_id, session_id, student_code, verdict, judge_results, status, created_at "
            "FROM submissions WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall())

        result = []
        for row in rows:
            sub = DBSubmission(**dict(row))
            result.append(sub.to_dict())
        logger.info("get_submissions_by_session() — session=%s, %d rows", session_id, len(result))
        return result
    except Exception as exc:
        logger.error("get_submissions_by_session(%s) failed: %s", session_id, exc)
        raise


def get_all_problem_verdicts(user_id: str | None = None) -> dict[int, str]:
    """Return the latest verdict for every problem that has submissions.

    Returns dict mapping problem_id → verdict (e.g. {1: 'AC', 2: 'WA'}).
    user_id 非空时按用户过滤（多用户隔离：题库共享、进度各自独立）。
    """
    try:
        sql = (
            "SELECT s.problem_id, s.verdict FROM submissions s "
            "JOIN (SELECT problem_id, MAX(id) AS max_id FROM submissions"
        )
        params: list[Any] = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params.append(user_id)
        sql += (
            " GROUP BY problem_id) latest "
            "ON s.problem_id = latest.problem_id AND s.id = latest.max_id"
        )
        rows = _with_conn(lambda cursor: cursor.execute(sql, params).fetchall())
        return {row[0]: row[1] for row in rows if row[1]}
    except Exception as exc:
        logger.error("get_all_problem_verdicts() failed: %s", exc)
        return {}


def get_unac_problem(
    topic: str | None = None,
    difficulty: str | None = None,
    profile_hint: str | None = None,
    exclude_ids: set[int] | None = None,
) -> Optional[int]:
    """返回一道「历史做过但未 AC」的题目 id（HISTORY 兜底通道）。

    优先级（在未 AC 的题目中）：
    1. topic / profile_hint 命中的题目（用户指定 topic 优先：topic > 弱项 profile_hint）
    2. difficulty 命中
    3. 最近提交的题目

    无未 AC 题目或查询失败返回 None。

    Args:
        exclude_ids: 排除的题目 id 集合（通常含当前会话正在做的题），避免把
            刚失败的那道又捞回来重复出。
    """
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT s.problem_id, p.topic, p.difficulty, s.id AS sub_id "
            "FROM submissions s "
            "JOIN (SELECT problem_id, MAX(id) AS max_id FROM submissions GROUP BY problem_id) latest "
            "ON s.problem_id = latest.problem_id AND s.id = latest.max_id "
            "JOIN problems p ON p.id = s.problem_id "
            "WHERE s.verdict IS NOT NULL AND s.verdict != 'AC' "
            + ("AND s.problem_id NOT IN (%s) " % ",".join("?" * len(exclude_ids)) if exclude_ids else "")
            + "ORDER BY s.id DESC",
            tuple(exclude_ids) if exclude_ids else (),
        ).fetchall())
        if not rows:
            return None

        def _score(row) -> int:
            s = 0
            # 用户指定 topic 优先于弱项 profile_hint：topic 命中最先 +8，
            # 仅当 topic 未命中时才回退到 profile_hint（避免弱项覆盖用户明确意图）。
            if topic and (row["topic"] == topic or topic in row["topic"]):
                s += 8
            elif profile_hint and (row["topic"] == profile_hint or profile_hint in row["topic"]):
                s += 8
            if difficulty and row["difficulty"] == difficulty:
                s += 4
            return s

        best = max(rows, key=_score)
        return best["problem_id"]
    except Exception as exc:
        logger.error("get_unac_problem() failed: %s", exc)
        return None


def get_existing_norm_ids(exclude: set[int] | None = None) -> dict[str, int]:
    """返回库中已存在题目的 ``(starter_code_norm, id)`` 映射（静态兜底预检用）。

    静态兜底盲选 ``random.choice`` 会抽到用户已做过/正在做的题；这里把库里
    已有题目（按归一化形态）的 norm→id 暴露给静态池，使其能避开这些题。

    Args:
        exclude: 排除的 id 集合（当前会话已出现的题），不计入"已存在"。
    """
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, starter_code_norm FROM problems WHERE starter_code_norm <> ''"
        ).fetchall())
        out: dict[str, int] = {}
        for r in rows:
            pid = r["id"]
            if exclude and pid in exclude:
                continue
            out.setdefault(r["starter_code_norm"], pid)
        return out
    except Exception as exc:
        logger.error("get_existing_norm_ids() failed: %s", exc)
        return {}


# ── 会话活跃时间（TTL 自动清理）──


def touch_session(session_id: str, user_id: str = "default") -> None:
    """记录会话的最后活跃时间（upsert）。

    每次用户操作（chat、submit、poll state 等）时调用。
    user_id 只在首次插入时写入（会话归属不可变），冲突时仅刷新活跃时间。
    """
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO session_activity (session_id, user_id, last_active_at) "
            "VALUES (?, ?, LOCALTIMESTAMP) "
            "ON CONFLICT(session_id) DO UPDATE SET last_active_at = LOCALTIMESTAMP",
            (session_id, user_id),
        ))
    except Exception as exc:
        logger.warning("touch_session(%s) failed: %s", session_id, exc)


def get_session_owner(session_id: str) -> str | None:
    """查询会话归属 user_id；会话不存在返回 None。

    多用户越权校验的事实源。返回 None（存量旧会话无归属）时调用方按
    「遗留数据放行」处理——只认 sid 的旧行为，避免历史会话全体失联。
    """
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT user_id FROM session_activity WHERE session_id = ?",
            (session_id,),
        ).fetchone())
        if row is None:
            return None
        return row["user_id"]
    except Exception as exc:
        logger.warning("get_session_owner(%s) failed: %s", session_id, exc)
        return None


def get_session_ids_for_user(user_id: str) -> set[str]:
    """返回某用户的全部会话 id（会话列表按归属过滤用）。"""
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT session_id FROM session_activity WHERE user_id = ?",
            (user_id,),
        ).fetchall())
        return {row["session_id"] for row in rows}
    except Exception as exc:
        logger.error("get_session_ids_for_user(%s) failed: %s", user_id, exc)
        return set()


def get_stale_sessions(max_age_hours: int) -> list[str]:
    """返回超过 max_age_hours 未活跃的 session_id 列表。"""
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT session_id FROM session_activity "
            "WHERE last_active_at < LOCALTIMESTAMP - (%s::text || ' hours')::interval",
            (str(max_age_hours),),
        ).fetchall())
        return [row["session_id"] for row in rows]
    except Exception as exc:
        logger.error("get_stale_sessions(%d) failed: %s", max_age_hours, exc)
        return []


def delete_session_activity(session_id: str) -> None:
    """从 session_activity 表中删除一条记录。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "DELETE FROM session_activity WHERE session_id = ?",
            (session_id,),
        ))
    except Exception as exc:
        logger.warning("delete_session_activity(%s) failed: %s", session_id, exc)


def delete_session_sidecar_data(session_id: str) -> None:
    """清理某会话的全部旁路数据（与 checkpointer.delete_thread 联动，随会话 TTL 清理）。

    覆盖：edit_traces / trace_analysis / analysis_results / trace_summaries /
    trace_threads / submissions（均按 session_id 为 key 持续增长，无 TTL）。
    任一失败只告警不阻断主删除流程。
    """
    try:
        def _do(cursor):
            for table in ("edit_traces", "trace_analysis", "analysis_results",
                          "trace_summaries", "trace_threads"):
                cursor.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM submissions WHERE session_id = ?", (session_id,))
        _with_conn(_do)
        logger.info("delete_session_sidecar_data() — session=%s", session_id)
    except Exception as exc:
        logger.warning("delete_session_sidecar_data(%s) failed: %s", session_id, exc)


def get_all_submissions(limit: int = 100, user_id: str | None = None) -> list[dict]:
    """Return recent submissions (for admin panel / 个人中心).

    ``user_id`` 传 None 时返回全部用户（管理端）；传具体 uid 只返回该用户的
    提交（个人中心「我的提交」）。存量旧数据 user_id='default' 天然被过滤。
    """
    try:
        if user_id is not None:
            rows = _with_conn(lambda cursor: cursor.execute(
                "SELECT s.id, s.problem_id, p.title AS problem_title, s.verdict, s.student_code, s.created_at "
                "FROM submissions s LEFT JOIN problems p ON s.problem_id = p.id "
                "WHERE s.user_id = ? "
                "ORDER BY s.id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall())
        else:
            rows = _with_conn(lambda cursor: cursor.execute(
                "SELECT s.id, s.problem_id, p.title AS problem_title, s.verdict, s.student_code, s.created_at "
                "FROM submissions s LEFT JOIN problems p ON s.problem_id = p.id "
                "ORDER BY s.id DESC LIMIT ?",
                (limit,),
            ).fetchall())
        return [{
            "id": row[0],
            "problem_id": row[1],
            "problem_title": row[2] or f"Problem #{row[1]}",
            "verdict": row[3],
            "code": row[4],
            "created_at": row[5],
        } for row in rows]
    except Exception as exc:
        logger.error("get_all_submissions() failed: %s", exc)
        return []


# ── User profile ──

# ── User profile ──


def get_profile(user_id: str = "default") -> "DBProfile":
    """Read the user profile from the profiles table.

    返回 ``DBProfile`` 对象（内部消费方按属性访问、并传给 save_profile 做
    ``model_dump_json``）。``ac_rate`` 每次从 submissions 现算并写到对象上，
    供 ``/admin/profile`` 直接序列化给前端。无记录 / 出错均返回默认对象。
    """
    import json as _json
    from .models import DBProfile
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT profile_json FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone())

        if not row:
            logger.info("get_profile() — no profile for '%s', returning defaults", user_id)
            profile = DBProfile()
        else:
            data = _json.loads(row["profile_json"])
            profile = DBProfile(**data)

        # Compute AC rate from submissions table.
        # submissions has no user_id column — count across all rows (single-user mode).
        sub_rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT verdict FROM submissions"
        ).fetchall())
        total_cnt = len(sub_rows)
        ac_cnt = sum(1 for r in sub_rows if r[0] == "AC")
        profile.ac_rate = round(ac_cnt / total_cnt * 100, 1) if total_cnt else 0.0
        return profile
    except Exception as exc:
        logger.error("get_profile(%s) failed: %s", user_id, exc)
        return DBProfile()


def save_profile(profile, user_id: str = "default"):
    """Save (upsert) a user profile to the profiles table."""
    import json as _json
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO profiles (user_id, profile_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET profile_json = excluded.profile_json, updated_at = LOCALTIMESTAMP",
            (user_id, profile.model_dump_json()),
        ))
        logger.info("save_profile() — user=%s, proficiency=%.2f, attempts=%d",
                     user_id, profile.proficiency, profile.attempts)
    except Exception as exc:
        logger.error("save_profile(%s) failed: %s", user_id, exc)
        raise


def update_profile_on_result(
    topic: str,
    verdict: str,
    error_type: str = "",
    user_id: str = "default",
):
    """Update the user profile after a judge result (AC, WA, or give-up)."""
    from .models import DBProfile
    profile = get_profile(user_id)
    profile.attempts += 1
    profile.forget_days = 0

    alpha = 0.3
    if verdict == "AC":
        profile.proficiency = profile.proficiency + alpha * (1.0 - profile.proficiency)
        profile.stability = min(1.0, profile.stability + 0.05)
    elif verdict in ("WA", "TLE", "RE", "give_up"):
        profile.proficiency = profile.proficiency - alpha * profile.proficiency
        profile.proficiency = max(0.0, profile.proficiency)
        profile.stability = max(0.0, profile.stability - 0.1)
        if error_type:
            if error_type not in profile.common_errors:
                profile.common_errors.append(error_type)
                profile.common_errors = profile.common_errors[-10:]

    save_profile(profile, user_id)
    logger.info("update_profile(%s) → verdict=%s, proficiency=%.2f, attempts=%d",
                topic, verdict, profile.proficiency, profile.attempts)
    return profile


# ── 编辑轨迹 + 错误模式画像（error-mode-tracking 特性）──


def save_edit_trace(session_id: str, user_id: str, events: list[dict], problem_id: str = "default") -> None:
    """累计保存某会话的编辑轨迹事件（UPSERT + 追加），按 (session_id, problem_id) 联合主键隔离。

    frontend 每次 flush 只发送自上次 flush 以来的增量事件；后端读旧 →
    追加 → 写回，保证多次 flush 的事件不丢。整个读改写在一个连接事务内完成。
    事件按内嵌 problem_id 分组后写入各自行；无内嵌 pid 的事件用请求体 problem_id 兜底。
    每行 events_json 只存该题的事件，不再每条事件冗余 problem_id（行级 problem_id 已是主键）。
    """
    problem_id = str(problem_id) if problem_id is not None else "default"
    # 按事件内嵌 pid 分组，去除冗余的 per-event problem_id（行级 pid 已是联合主键）
    groups: dict[str, list[dict]] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        pid = str(e.get("problem_id") or problem_id)
        ev = {k: v for k, v in e.items() if k != "problem_id"}
        groups.setdefault(pid, []).append(ev)

    if not groups:
        groups[problem_id] = []

    def _do(cursor):
        for pid, evs in groups.items():
            row = cursor.execute(
                "SELECT events_json FROM edit_traces WHERE session_id = ? AND problem_id = ?",
                (session_id, pid)
            ).fetchone()
            if row:
                old = json.loads(row["events_json"] or "[]")
                merged = old + evs
                cursor.execute(
                    "UPDATE edit_traces SET events_json = ?, user_id = ?, updated_at = LOCALTIMESTAMP "
                    "WHERE session_id = ? AND problem_id = ?",
                    (json.dumps(merged, ensure_ascii=False), user_id, session_id, pid),
                )
            else:
                cursor.execute(
                    "INSERT INTO edit_traces (session_id, user_id, problem_id, events_json) VALUES (?, ?, ?, ?)",
                    (session_id, user_id, pid, json.dumps(evs, ensure_ascii=False)),
                )
    try:
        _with_conn(_do)
        total = sum(len(evs) for evs in groups.values())
        logger.info("save_edit_trace() — session=%s, +%d events across %d problems", session_id, total, len(groups))
    except Exception as exc:
        logger.error("save_edit_trace(%s) failed: %s", session_id, exc)
        raise


def _apply_code_diff(base_code: str, diff_text: str) -> str:
    """按行级 diff 文本重建全量代码。

    diff 格式（前端 useEditTrace 生成）：
      # a0-a1 -> b0-b1      # 行区间（0 起，与 split('\\n') 索引一致）
      -旧行                # 仅标注，应用时不需要
      +新行
    从后往前替换 lines[a0:a1] = new_lines，避免行号偏移。
    """
    lines = base_code.split("\n")
    hunks: list[tuple[int, int, list[str]]] = []
    cur_hunk: tuple[int, int, list[str]] | None = None
    for ln in diff_text.split("\n"):
        if ln.startswith("# "):
            if cur_hunk is not None:
                hunks.append(cur_hunk)
            head = ln[2:].split(" -> ")
            a0, a1 = (int(x) for x in head[0].split("-"))
            cur_hunk = (a0, a1, [])
        elif ln.startswith("+") and cur_hunk is not None:
            cur_hunk[2].append(ln[1:])
    if cur_hunk is not None:
        hunks.append(cur_hunk)
    for a0, a1, new_lines in reversed(hunks):
        lines[a0:a1] = new_lines
    return "\n".join(lines)


def reconstruct_edit_trace(events: list[dict]) -> list[dict]:
    """把事件流重建为带全量 code 的事件列表（返回新列表，不改 DB）。

    全量方案（新数据）：每个 edit/run/submit 已自带全量 code；only same_as_prev 事件
    （与上一条完全相同）未携带 code，此处直接继承上一条的 code（纯去重，不丢真相）。
    不再有 diff 链脆弱性：单点丢失只丢那一条、绝不传染半场；存储真相即代码本身。

    diff 分支（旧数据兼容）：code_format='diff' 的事件相对"上一快照"增量存储；
    若链断（前面缺少全量基准，如事件丢失），该事件无法重建 → 丢弃并计数告警。

    P2-2: 按 (ts, seq) 稳定排序，消除同 ts 事件的顺序歧义。
    """
    events = sorted(events, key=lambda e: (e.get("ts", 0), e.get("seq", 0) or 0))
    out: list[dict] = []
    last_code: Optional[str] = None
    dropped = 0
    for ev in events:
        ev = dict(ev)
        if ev.get("code_format") == "diff":
            # 旧数据兼容：行级 diff 重建
            if last_code is None:
                dropped += 1
                continue
            ev["code"] = _apply_code_diff(last_code, ev.get("code_diff") or "")
            ev.pop("code_format", None)
            ev.pop("code_diff", None)
            last_code = ev["code"]
        elif ev.get("same_as_prev"):
            # 全量方案去重事件：继承上一条 code（若首条就 same_as_prev 则 code=None，罕见）
            if last_code is not None:
                ev["code"] = last_code
        elif ev.get("code") is not None:
            last_code = ev["code"]
        out.append(ev)
    if dropped:
        logger.warning("reconstruct_edit_trace: %d 条 diff 事件因缺少全量基准被丢弃", dropped)
    return out


def get_edit_trace(session_id: str) -> list[dict]:
    """读取某会话的编辑轨迹事件列表（合并全部题）。无记录 / 出错均返回 []。

    按 (session_id, problem_id) 联合主键存储：本函数合并该会话全部题的事件。
    diff 事件（code_format='diff'）在读取时按序重建为全量 code 快照，
    下游（LLM 分析 / 画像）拿到的始终是带全量代码的事件。
    """
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT events_json FROM edit_traces WHERE session_id = ?", (session_id,)
        ).fetchall())
        if not rows:
            return []
        all_events: list[dict] = []
        for row in rows:
            all_events.extend(json.loads(row["events_json"] or "[]"))
        return reconstruct_edit_trace(all_events)
    except Exception as exc:
        logger.error("get_edit_trace(%s) failed: %s", session_id, exc)
        return []


def save_trace_analysis(session_id: str, result: dict) -> None:
    """缓存某会话的独立轨迹分析结论（UPSERT，按 session 维度）。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO trace_analysis (session_id, result_json) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "result_json = excluded.result_json, created_at = LOCALTIMESTAMP",
            (session_id, json.dumps(result, ensure_ascii=False)),
        ))
        logger.info("save_trace_analysis() — session=%s", session_id)
    except Exception as exc:
        logger.error("save_trace_analysis(%s) failed: %s", session_id, exc)
        raise


def get_trace_analysis(session_id: str) -> Optional[dict]:
    """读取某会话已缓存的轨迹分析结论。无记录 / 出错均返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT result_json FROM trace_analysis WHERE session_id = ?", (session_id,)
        ).fetchone())
        if not row:
            return None
        return json.loads(row["result_json"] or "{}")
    except Exception as exc:
        logger.error("get_trace_analysis(%s) failed: %s", session_id, exc)
        return None


def get_edit_trace_by_problem(session_id: str, problem_id: Optional[str] = None) -> list[dict]:
    """读取某会话指定题的编辑轨迹事件。

    按 (session_id, problem_id) 联合主键直接查询，无需 JSON 数组过滤。
    problem_id 为 None / "default" 时返回全部题的事件（兼容旧用法）。
    """
    if not problem_id or problem_id == "default":
        return get_edit_trace(session_id)
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT events_json FROM edit_traces WHERE session_id = ? AND problem_id = ?",
            (session_id, str(problem_id))
        ).fetchone())
        if not row:
            return []
        return reconstruct_edit_trace(json.loads(row["events_json"] or "[]"))
    except Exception as exc:
        logger.error("get_edit_trace_by_problem(%s, %s) failed: %s", session_id, problem_id, exc)
        return []


def purge_trace_data(days: int = 30, dry_run: bool = False) -> dict:
    """清理过期的细粒度轨迹数据（全量方案下 edit_traces 增长较快，需定期瘦身）。

    只删「轨迹分析派生数据」，绝不碰 submissions / profiles / problems 等核心业务表：
    - edit_traces      ：前端全量采集的细粒度编辑事件（最大头）
    - trace_threads    ：多轮分析线程 transcript
    - trace_analysis    ：首轮结构化结论缓存
    - analysis_results  ：按题分析结果
    - trace_summaries   ：过渡摘要

    删除依据：各表 updated_at / created_at < (now - days)。默认保留 30 天。
    返回被删行数统计，便于观测与审计。

    Args:
        days: 保留天数，早于 (now - days) 的行会被清理，默认 30。
        dry_run: 为 True 时只统计待删行数、不执行 DELETE（供 CLI 预览复用同一份
            targets 定义，避免表/列名在两处漂移）。默认 False，即真实删除。

    触发方式（二选一，均已在设计文档约定）：
    - 管理端点  GET /admin/purge-trace?days=30  （运维手动触发）
    - 定时任务  （cron / 启动后后台线程，按业务节奏调用本函数）
    """
    cutoff = f"LOCALTIMESTAMP - INTERVAL '{int(days)} days'"
    targets = [
        ("edit_traces", "updated_at"),
        ("trace_threads", "updated_at"),
        ("trace_analysis", "created_at"),
        ("analysis_results", "updated_at"),
        ("trace_summaries", "created_at"),
    ]
    stats: dict[str, int] = {}
    try:
        for table, col in targets:
            if dry_run:
                n = _with_conn(lambda cursor, t=table, c=col: cursor.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE {c} < {cutoff}"
                ).fetchone()[0])
            else:
                n = _with_conn(lambda cursor, t=table, c=col: cursor.execute(
                    f"DELETE FROM {t} WHERE {c} < {cutoff}"
                ).rowcount)
            stats[table] = n
        logger.info("purge_trace_data: %s %d 天前轨迹数据 -> %s",
                    "预演统计" if dry_run else "清理", days, stats)
    except Exception as exc:
        logger.error("purge_trace_data(%d) failed: %s", days, exc)
    return stats


# ── 监控告警：alert_history（docs/monitoring-alerts-design.md §8）──

def save_alert(
    rule_id: str,
    severity: str,
    status: str,
    title: str,
    detail: str | None = None,
    notified: bool = False,
) -> int:
    """落一条告警历史。返回行 id；失败返回 0（告警落库绝不外抛）。"""
    try:
        def _do(cursor):
            cursor.execute(
                "INSERT INTO alert_history "
                "(rule_id, severity, status, title, detail, triggered_at, notified) "
                "VALUES (?, ?, ?, ?, ?, LOCALTIMESTAMP::text, ?) RETURNING id",
                (rule_id, severity, status, title, detail, 1 if notified else 0),
            )
            return int(cursor.lastrowid or 0)
        return _with_conn(_do)
    except Exception as exc:
        logger.warning("save_alert(%s) failed (non-fatal): %s", rule_id, exc)
        return 0


def get_recent_alerts(limit: int = 50) -> list[dict]:
    """告警历史，倒序。失败返回空列表。"""
    try:
        def _do(cursor):
            cursor.execute(
                "SELECT id, rule_id, severity, status, title, detail, triggered_at, notified "
                "FROM alert_history ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cursor.fetchall()]
        return _with_conn(_do)
    except Exception as exc:
        logger.warning("get_recent_alerts failed (non-fatal): %s", exc)
        return []


def purge_alerts(days: int = 90) -> int:
    """清理过期告警历史（默认保留 90 天），返回删除行数。"""
    try:
        cutoff = f"LOCALTIMESTAMP - INTERVAL '{int(days)} days'"
        n = _with_conn(lambda cursor: cursor.execute(
            f"DELETE FROM alert_history WHERE triggered_at < ({cutoff})::text"
        ).rowcount)
        if n:
            logger.info("purge_alerts: cleaned %d rows older than %dd", n, days)
        return n
    except Exception as exc:
        logger.warning("purge_alerts failed (non-fatal): %s", exc)
        return 0


# ── 主页公告横幅（docs/monitoring-alerts-design.md §15）──

def _deactivate_monitor_rows(cursor, rule_id: str) -> None:
    """把某规则的历史 monitor 公告全部置 inactive（自动横幅：一次一条）。"""
    cursor.execute(
        "UPDATE announcements SET active = 0 "
        "WHERE source = 'monitor' AND rule_id = ? AND active = 1",
        (rule_id,),
    )


def upsert_monitor_announcement(rule_id: str, level: str, title: str, content: str) -> None:
    """监控触发 firing 时同步一条自动横幅（同规则旧横幅失效，一次只挂一条）。"""
    try:
        def _do(cursor):
            _deactivate_monitor_rows(cursor, rule_id)
            cursor.execute(
                "INSERT INTO announcements "
                "(level, title, content, source, rule_id, active, created_at) "
                "VALUES (?, ?, ?, 'monitor', ?, 1, LOCALTIMESTAMP::text)",
                (level, title, content, rule_id),
            )
        _with_conn(_do)
    except Exception as exc:
        logger.warning("upsert_monitor_announcement(%s) failed (non-fatal): %s", rule_id, exc)


def deactivate_monitor_announcement(rule_id: str) -> None:
    """告警 resolved 时撤下自动横幅。"""
    try:
        _with_conn(lambda cursor: _deactivate_monitor_rows(cursor, rule_id))
    except Exception as exc:
        logger.warning("deactivate_monitor_announcement(%s) failed (non-fatal): %s", rule_id, exc)


def create_announcement(
    level: str,
    title: str,
    content: str,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> int:
    """管理员手动公告。返回行 id；失败返回 0。"""
    try:
        def _do(cursor):
            cursor.execute(
                "INSERT INTO announcements "
                "(level, title, content, source, active, starts_at, ends_at, created_at) "
                "VALUES (?, ?, ?, 'admin', 1, ?, ?, LOCALTIMESTAMP::text) RETURNING id",
                (level, title, content, starts_at, ends_at),
            )
            return int(cursor.lastrowid or 0)
        return _with_conn(_do)
    except Exception as exc:
        logger.warning("create_announcement failed (non-fatal): %s", exc)
        return 0


def deactivate_announcement(announcement_id: int) -> bool:
    """下线公告（admin 手动或通用）。返回是否命中。"""
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE announcements SET active = 0 WHERE id = ?",
                (int(announcement_id),),
            )
            return cursor.rowcount > 0
        return bool(_with_conn(_do))
    except Exception as exc:
        logger.warning("deactivate_announcement(%s) failed (non-fatal): %s", announcement_id, exc)
        return False


def deactivate_all_monitor_announcements() -> int:
    """watcher 启动时清空全部 monitor 横幅（进程重启 = 告警状态机重置，横幅一并撤下）。

    admin 手动公告不受影响。返回撤下行数。
    """
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE announcements SET active = 0 WHERE source = 'monitor' AND active = 1"
            )
            return cursor.rowcount
        return int(_with_conn(_do))
    except Exception as exc:
        logger.warning("deactivate_all_monitor_announcements failed (non-fatal): %s", exc)
        return 0


def get_today_token_cost() -> float:
    """今日 token 总成本（元）。按 token_usage.ts 本地日期聚合；失败返回 0。"""
    try:
        row = _with_conn(lambda c: c.execute(
            "SELECT COALESCE(SUM(cost),0) FROM token_usage "
            "WHERE ts::date = LOCALTIMESTAMP::date"
        ).fetchone())
        return float(row[0] or 0)
    except Exception as exc:
        logger.debug("get_today_token_cost failed (non-fatal): %s", exc)
        return 0.0


def get_active_announcements(now: str | None = None) -> list[dict]:
    """当前生效中的公告（admin 公告按生效窗口过滤，monitor 公告按 active）。

    now: ISO 格式 'YYYY-MM-DD HH:MM:SS'（本地时区），None 则取当前时间。
    """
    try:
        from datetime import datetime  # 本文件 datetime 在末尾聚合区导入，此处就地取
        if now is None:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _do(cursor):
            cursor.execute(
                "SELECT id, level, title, content, source, rule_id, starts_at, ends_at, created_at "
                "FROM announcements WHERE active = 1 "
                "AND (starts_at IS NULL OR starts_at = '' OR starts_at <= ?) "
                "AND (ends_at IS NULL OR ends_at = '' OR ends_at >= ?) "
                "ORDER BY id DESC LIMIT 20",
                (now, now),
            )
            return [dict(r) for r in cursor.fetchall()]
        return _with_conn(_do)
    except Exception as exc:
        logger.warning("get_active_announcements failed (non-fatal): %s", exc)
        return []


def save_trace_thread(session_id: str, problem_id: str, messages: list[dict]) -> None:
    """持久化某题分析线程的消息列表（UPSERT，按 (session, problem) 维度）。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO trace_threads (session_id, problem_id, messages_json, updated_at) "
            "VALUES (?, ?, ?, LOCALTIMESTAMP) "
            "ON CONFLICT(session_id, problem_id) DO UPDATE SET "
            "messages_json = excluded.messages_json, updated_at = LOCALTIMESTAMP",
            (session_id, problem_id, json.dumps(messages, ensure_ascii=False)),
        ))
        logger.info("save_trace_thread() — session=%s pid=%s msgs=%d", session_id, problem_id, len(messages))
    except Exception as exc:
        logger.error("save_trace_thread(%s,%s) failed: %s", session_id, problem_id, exc)
        raise


def get_trace_thread(session_id: str, problem_id: str) -> Optional[list[dict]]:
    """读取某题分析线程的消息列表。无记录 / 出错均返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT messages_json FROM trace_threads WHERE session_id = ? AND problem_id = ?",
            (session_id, problem_id),
        ).fetchone())
        if not row:
            return None
        return json.loads(row["messages_json"] or "[]")
    except Exception as exc:
        logger.error("get_trace_thread(%s,%s) failed: %s", session_id, problem_id, exc)
        return None


def delete_trace_thread(session_id: str, problem_id: str) -> None:
    """删除某题分析线程（过渡归档时调用）。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "DELETE FROM trace_threads WHERE session_id = ? AND problem_id = ?",
            (session_id, problem_id),
        ))
        logger.info("delete_trace_thread() — session=%s pid=%s", session_id, problem_id)
    except Exception as exc:
        logger.error("delete_trace_thread(%s,%s) failed: %s", session_id, problem_id, exc)


def save_analysis_result(session_id: str, problem_id: str, result: dict, model: str = "") -> None:
    """缓存某题的轨迹分析首轮结构化结论（UPSERT，按 (session, problem) 维度）。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO analysis_results (session_id, problem_id, result_json, model, updated_at) "
            "VALUES (?, ?, ?, ?, LOCALTIMESTAMP) "
            "ON CONFLICT(session_id, problem_id) DO UPDATE SET "
            "result_json = excluded.result_json, model = excluded.model, updated_at = LOCALTIMESTAMP",
            (session_id, problem_id, json.dumps(result, ensure_ascii=False), model),
        ))
        logger.info("save_analysis_result() — session=%s pid=%s", session_id, problem_id)
    except Exception as exc:
        logger.error("save_analysis_result(%s,%s) failed: %s", session_id, problem_id, exc)
        raise


def get_analysis_result(session_id: str, problem_id: str) -> Optional[dict]:
    """读取某题已缓存的轨迹分析首轮结论。无记录 / 出错均返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT result_json FROM analysis_results WHERE session_id = ? AND problem_id = ?",
            (session_id, problem_id),
        ).fetchone())
        if not row:
            return None
        return json.loads(row["result_json"] or "{}")
    except Exception as exc:
        logger.error("get_analysis_result(%s,%s) failed: %s", session_id, problem_id, exc)
        return None


def save_trace_summary(
    session_id: str, problem_id: str, transition_action: str, summary: dict, token_est: int = 0
) -> None:
    """缓存某题的过渡摘要（UPSERT，按 (session, problem) 覆盖取最新）。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO trace_summaries (session_id, problem_id, transition_action, summary_json, token_est) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, problem_id) DO UPDATE SET "
            "transition_action = excluded.transition_action, summary_json = excluded.summary_json, "
            "token_est = excluded.token_est, created_at = LOCALTIMESTAMP",
            (session_id, problem_id, transition_action, json.dumps(summary, ensure_ascii=False), token_est),
        ))
        logger.info("save_trace_summary() — session=%s pid=%s action=%s", session_id, problem_id, transition_action)
    except Exception as exc:
        logger.error("save_trace_summary(%s,%s) failed: %s", session_id, problem_id, exc)
        raise


def get_trace_summary(session_id: str, problem_id: str) -> Optional[dict]:
    """读取某题的过渡摘要。无记录 / 出错均返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT summary_json FROM trace_summaries WHERE session_id = ? AND problem_id = ?",
            (session_id, problem_id),
        ).fetchone())
        if not row:
            return None
        return json.loads(row["summary_json"] or "{}")
    except Exception as exc:
        logger.error("get_trace_summary(%s,%s) failed: %s", session_id, problem_id, exc)
        return None


def apply_error_mode_deltas(
    user_id: str,
    deltas: list,
    verdict_boost: bool = False,
) -> dict:
    """把错误模式增量合并进用户的 DBProfile.error_modes（命中衰减 + 加权合并 + 封顶）。

    Args:
        user_id: 画像所属用户（单用户模式为 "default"）。错误模式画像挂在
            DBProfile 上（V1 全局层），所以按 user_id 而非 session_id 写入。
        deltas: list[ErrorModeDelta]（来自编辑轨迹分析或判题失败补充 feeder）。
        verdict_boost: True 时对 deltas 整体 ×1.3 再合并（判题失败补充 feeder）。

    Returns:
        合并后的 error_modes dict。
    """
    from code_tutor_agent.profile.weakness import apply_deltas, boost_verdict_deltas

    profile = get_profile(user_id)
    if verdict_boost:
        deltas = boost_verdict_deltas(deltas)
    new_modes = apply_deltas(profile.error_modes, deltas)
    profile.error_modes = new_modes
    save_profile(profile, user_id)
    logger.info("apply_error_mode_deltas() — user=%s, dims=%d, verdict_boost=%s",
                user_id, len(new_modes), verdict_boost)
    return new_modes


# ── User profile v2 (per-tag, from profile module) ──


def save_user_profile_v2(profile: dict, user_id: str = "default_v2") -> None:
    """Save the new per-tag UserProfile to the profiles table."""
    import json as _json
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO profiles (user_id, profile_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET profile_json = excluded.profile_json, updated_at = LOCALTIMESTAMP",
            (user_id, _json.dumps(profile, ensure_ascii=False)),
        ))
        logger.info("save_user_profile_v2() — user=%s, tags=%d", user_id, len(profile.get("prof", {})))
    except Exception as exc:
        logger.error("save_user_profile_v2(%s) failed: %s", user_id, exc)
        raise


def get_user_profile_v2(user_id: str = "default_v2") -> dict:
    """Read the new per-tag UserProfile from the profiles table.

    Returns ALL known tags (35 from Tag enum), filling zero scores for
    tags the user hasn't practiced yet. Also attaches ``tag_names`` for
    frontend display.
    """
    import json as _json
    from code_tutor_agent.profile.tags import Tag

    # ── 中文显示名 ──
    TAG_DISPLAY: dict[str, str] = {
        "array_basics": "数组基础",
        "array_two_pointers": "双指针",
        "array_sliding_window": "滑动窗口",
        "array_binary_search": "二分查找",
        "array_prefix_sum": "前缀和",
        "array_sorting": "排序",
        "linkedlist_basics": "链表基础",
        "linkedlist_two_pointers": "链表双指针",
        "linkedlist_cycle": "环检测",
        "stack_basics": "栈基础",
        "queue_deque": "队列/双端队列",
        "monotonic_stack": "单调栈",
        "heap_priority_queue": "堆/优先队列",
        "tree_dfs": "树 DFS",
        "tree_bfs": "树 BFS",
        "tree_bst": "二叉搜索树",
        "graph_dfs": "图 DFS",
        "graph_bfs": "图 BFS",
        "graph_topo": "拓扑排序",
        "union_find": "并查集",
        "dp_1d": "一维 DP",
        "dp_multidim": "多维 DP",
        "dp_interval": "区间 DP",
        "dp_tree": "树形 DP",
        "string_basics": "字符串基础",
        "string_pattern": "字符串匹配",
        "string_dp": "字符串 DP",
        "backtrack": "回溯",
        "greedy": "贪心",
        "bit_manip": "位运算",
        "math_number_theory": "数论",
        "design": "设计",
    }

    all_tags = Tag.all_values()

    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT profile_json FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone())

        if not row:
            profile = {"prof": {}, "prof_elo_raw": {}, "stab": {}, "forget": {}, "errors": {"_global": {}, "per_tag": {}}, "attempts": {}, "meta": {"schema_version": "mvp@1"}}
        else:
            profile = _json.loads(row["profile_json"])

        # 补全零分 tag
        for field in ("prof", "prof_elo_raw"):
            if field not in profile:
                profile[field] = {}
            for tag in all_tags:
                profile[field].setdefault(tag, 0.0)

        for tag in all_tags:
            if "stab" not in profile:
                profile["stab"] = {}
            profile["stab"].setdefault(tag, {"window": [], "variance": 0.0})
            if "forget" not in profile:
                profile["forget"] = {}
            profile["forget"].setdefault(tag, {"last_seen": 0.0, "decay": 0.0})

        profile["tag_names"] = TAG_DISPLAY
        return profile
    except Exception as exc:
        logger.error("get_user_profile_v2(%s) failed: %s", user_id, exc)
        profile = {"prof": {}, "prof_elo_raw": {}, "stab": {}, "forget": {}, "errors": {"_global": {}, "per_tag": {}}, "attempts": {}, "meta": {"schema_version": "mvp@1"}}
        for tag in all_tags:
            profile["prof"].setdefault(tag, 0.0)
            profile["prof_elo_raw"].setdefault(tag, 0.0)
            profile["stab"].setdefault(tag, {"window": [], "variance": 0.0})
            profile["forget"].setdefault(tag, {"last_seen": 0.0, "decay": 0.0})
        profile["tag_names"] = TAG_DISPLAY
        return profile


# ── Agent memory(语义抽取式用户记忆,复用 profiles 表)──

MEMORY_USER_ID = "__memory__"


def get_user_memory(user_id: str = MEMORY_USER_ID) -> dict:
    """读取用户记忆 JSON。读不到 / 解析失败 → 返回空记忆,不抛错。"""
    import json as _json
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT profile_json FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone())
        if not row:
            return {}
        data = _json.loads(row["profile_json"])
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.error("get_user_memory(%s) failed: %s", user_id, exc)
        return {}


def save_user_memory(memory: dict, user_id: str = MEMORY_USER_ID) -> None:
    """保存用户记忆 JSON(upsert)。失败只记日志,不抛错——记忆不允许影响主流程。"""
    import json as _json
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO profiles (user_id, profile_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "profile_json = excluded.profile_json, updated_at = LOCALTIMESTAMP",
            (user_id, _json.dumps(memory, ensure_ascii=False)),
        ))
        logger.info("save_user_memory(%s) — behavior=%d, observations=%d",
                    user_id, len(memory.get("behavior", [])), len(memory.get("observations", [])))
    except Exception as exc:
        logger.warning("save_user_memory(%s) failed: %s", user_id, exc)


# ── 用户账号（邮箱密码登录，多用户支持）──


def create_user(email: str, password_hash: str, role: str = "user") -> int:
    """创建用户，返回新 id。邮箱重复抛 psycopg IntegrityError（由调用方转 409）。"""
    return _with_conn(lambda cursor: (
        cursor.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?) RETURNING id",
            (email, password_hash, role),
        ),
        cursor.lastrowid,
    )[1])


def get_user_by_email(email: str) -> Optional[dict]:
    """按邮箱查用户；不存在返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, email, password_hash, role FROM users WHERE email = ?",
            (email,),
        ).fetchone())
        return dict(row) if row else None
    except Exception as exc:
        logger.error("get_user_by_email(%s) failed: %s", email, exc)
        return None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """按 id 查用户；不存在返回 None。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, email, password_hash, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone())
        return dict(row) if row else None
    except Exception as exc:
        logger.error("get_user_by_id(%s) failed: %s", user_id, exc)
        return None


def count_users() -> int:
    """用户总数（首次注册引导 admin 用）。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone())
        return int(row["n"]) if row else 0
    except Exception as exc:
        logger.error("count_users() failed: %s", exc)
        return 0


def has_admin() -> bool:
    """是否已存在管理员账号（开放注册下第一个用户自动成为 admin 的判定依据）。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone())
        return row is not None
    except Exception as exc:
        logger.error("has_admin() failed: %s", exc)
        return False


# ── 用户管理（admin 重置密码 / 用户列表）──

def update_user_password(user_id: int, password_hash: str) -> bool:
    """更新用户密码哈希（自助改密 / admin 重置共用）。"""
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            return cursor.rowcount > 0
        ok = _with_conn(_do)
        logger.info("update_user_password(%s) → %s", user_id, ok)
        return ok
    except Exception as exc:
        logger.error("update_user_password(%s) failed: %s", user_id, exc)
        return False


def list_users() -> list[dict]:
    """用户列表（admin 面板）。不含密码哈希。"""
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, email, role, created_at FROM users ORDER BY id"
        ).fetchall())
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("list_users() failed: %s", exc)
        return []


# ── 邀请码（注册准入：额度 + 有效期）──

def create_invite_code(code: str, max_uses: int, expires_at: str | None, note: str = "") -> bool:
    """插入邀请码（code 由调用方生成；重复码返回 False）。"""
    try:
        def _do(cursor):
            cursor.execute(
                "INSERT INTO invite_codes (code, max_uses, expires_at, note) VALUES (?, ?, ?, ?)",
                (code, max_uses, expires_at, note),
            )
            return True
        return _with_conn(_do)
    except Exception as exc:
        logger.error("create_invite_code(%s) failed: %s", code, exc)
        return False


def consume_invite_code(code: str) -> bool:
    """原子校验并扣减邀请码额度。

    同时满足：码存在、active、未超额度、未过期 → used_count+1 并返回 True。
    任何一条不满足返回 False（幂等，不抛错）。
    """
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE invite_codes SET used_count = used_count + 1 "
                "WHERE code = ? AND active = 1 AND used_count < max_uses "
                "AND (expires_at IS NULL OR expires_at = '' OR expires_at::timestamp > LOCALTIMESTAMP)",
                (code,),
            )
            return cursor.rowcount == 1
        return _with_conn(_do)
    except Exception as exc:
        logger.error("consume_invite_code(%s) failed: %s", code, exc)
        return False


def list_invite_codes() -> list[dict]:
    """邀请码列表（admin 面板），按创建时间倒序。"""
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT code, max_uses, used_count, expires_at, active, note, created_at "
            "FROM invite_codes ORDER BY created_at DESC, code LIMIT 200"
        ).fetchall())
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("list_invite_codes() failed: %s", exc)
        return []


def set_invite_code_active(code: str, active: bool) -> bool:
    """启用/停用邀请码。"""
    try:
        def _do(cursor):
            cursor.execute(
                "UPDATE invite_codes SET active = ? WHERE code = ?",
                (1 if active else 0, code),
            )
            return cursor.rowcount > 0
        return _with_conn(_do)
    except Exception as exc:
        logger.error("set_invite_code_active(%s) failed: %s", code, exc)
        return False


# ── 密码重置码（忘记密码自助流程， Brevo 可选）──

def save_password_reset_code(email: str, code_hash: str, expires_at: str) -> None:
    """落一条重置码（同一邮箱旧码作废，防多码并存混乱）。"""
    def _do(cursor):
        cursor.execute("UPDATE password_reset_codes SET used = 1 WHERE email = ?", (email,))
        cursor.execute(
            "INSERT INTO password_reset_codes (email, code_hash, expires_at) VALUES (?, ?, ?)",
            (email, code_hash, expires_at),
        )
    try:
        _with_conn(_do)
    except Exception as exc:
        logger.error("save_password_reset_code(%s) failed: %s", email, exc)


def verify_password_reset_code(email: str, code_hash: str) -> bool:
    """校验重置码：存在、未用、未过期、哈希匹配。"""
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT 1 FROM password_reset_codes "
            "WHERE email = ? AND code_hash = ? AND used = 0 "
            "AND expires_at > LOCALTIMESTAMP LIMIT 1",
            (email, code_hash),
        ).fetchone())
        return row is not None
    except Exception as exc:
        logger.error("verify_password_reset_code(%s) failed: %s", email, exc)
        return False


def consume_password_reset_code(email: str, code_hash: str) -> None:
    """用后作废。"""
    try:
        _with_conn(lambda cursor: cursor.execute(
            "UPDATE password_reset_codes SET used = 1 "
            "WHERE email = ? AND code_hash = ? AND used = 0",
            (email, code_hash),
        ))
    except Exception as exc:
        logger.error("consume_password_reset_code(%s) failed: %s", email, exc)


# ── Token 用量(成本计量,见 docs/token-cost-control-design.md)──

from datetime import datetime, timedelta  # noqa: E402  (本文件末尾聚合查询专用)

_DATE_FMT = "%Y-%m-%d"


def _parse_date(d: str) -> datetime:
    return datetime.strptime(d, _DATE_FMT)


def _fmt_date(dt: datetime) -> str:
    return dt.strftime(_DATE_FMT)


def _date_n_days_ago(n: int) -> str:
    return _fmt_date(datetime.now() - timedelta(days=n))


def _days_between(f: str | None, t: str | None) -> int:
    if not f or not t:
        return 30
    return max(1, (_parse_date(t) - _parse_date(f)).days + 1)


def _shift_period(f: str, t: str, days: int) -> tuple[str, str]:
    """返回等长的前一个周期 [from, to](按天)。"""
    ft, tt = _parse_date(f), _parse_date(t)
    prev_to = ft - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    return _fmt_date(prev_from), _fmt_date(prev_to)


def _ts_filter(from_date: str | None, to_date: str | None,
               model_alias: str | None = None) -> tuple[list[str], list]:
    where, params = [], []
    if from_date:
        where.append("ts >= ?")
        params.append(from_date)
    if to_date:
        where.append("ts <= ?")
        params.append(to_date + " 23:59:59")
    # 模型筛选:"全部" 为空,传具体 alias 则限定
    if model_alias and model_alias != "全部":
        where.append("model_alias = ?")
        params.append(model_alias)
    return where, params


def insert_token_usage_batch(rows: list[tuple]) -> None:
    """批量写入 token_usage 明细(由异步 sink 调用)。

    ``ts`` 显式写入**本地**时间,与回调里的 ``ts_day``、查询用的
    ``_fmt_date(datetime.now())`` 保持同一时区基准。全库时间列已统一为
    ``LOCALTIMESTAMP``(SQLite 的 CURRENT_TIMESTAMP 是 UTC,
    跨时区会与「今日」过滤错配)。
    """
    if not rows:
        return
    try:
        _with_conn(lambda cursor: cursor.executemany(
            "INSERT INTO token_usage "
            "(ts,session_id,user_id,purpose,model_alias,model_name,mode,topic,difficulty,problem_id,"
            "prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,cost,latency_ms,run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(_local_ts(),) + row for row in rows],
        ))
    except Exception as exc:  # 落库失败绝不抛回主流程
        logger.error("insert_token_usage_batch() failed: %s", exc)


def _local_ts() -> str:
    """本地当前时间戳(YYYY-MM-DD HH:MM:SS),供 token 明细落库统一时区。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def upsert_token_usage_daily_batch(rows: list[tuple]) -> None:
    """按 (day,purpose,model_alias,user_id) UPSERT 预聚合。"""
    if not rows:
        return
    try:
        _with_conn(lambda cursor: cursor.executemany(
            "INSERT INTO token_usage_daily "
            "(day,purpose,model_alias,user_id,call_count,prompt_tokens,completion_tokens,"
            "cache_creation_tokens,cache_read_tokens,cost) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day,purpose,model_alias,user_id) DO UPDATE SET "
            "call_count=token_usage_daily.call_count+excluded.call_count, "
            "prompt_tokens=token_usage_daily.prompt_tokens+excluded.prompt_tokens, "
            "completion_tokens=token_usage_daily.completion_tokens+excluded.completion_tokens, "
            "cache_creation_tokens=token_usage_daily.cache_creation_tokens+excluded.cache_creation_tokens, "
            "cache_read_tokens=token_usage_daily.cache_read_tokens+excluded.cache_read_tokens, "
            "cost=token_usage_daily.cost+excluded.cost",
            rows,
        ))
    except Exception as exc:
        logger.error("upsert_token_usage_daily_batch() failed: %s", exc)


def _period_totals(from_date: str | None, to_date: str | None,
                   model_alias: str | None = None) -> dict:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = ("SELECT COALESCE(SUM(cost),0), COALESCE(SUM(1),0), COALESCE(SUM(prompt_tokens),0), "
           "COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cache_creation_tokens),0), "
           "COALESCE(SUM(cache_read_tokens),0) FROM token_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = _with_conn(lambda c: c.execute(sql, params).fetchone())
    return {
        "cost": float(row[0] or 0), "calls": int(row[1] or 0),
        "prompt": int(row[2] or 0), "completion": int(row[3] or 0),
        "cache_creation": int(row[4] or 0), "cache_read": int(row[5] or 0),
    }


def _purpose_totals(from_date: str | None, to_date: str | None,
                    model_alias: str | None = None) -> dict:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = ("SELECT purpose, COALESCE(SUM(cost),0), COALESCE(SUM(1),0), "
           "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
           "COALESCE(SUM(cache_creation_tokens),0), COALESCE(SUM(cache_read_tokens),0) "
           "FROM token_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY purpose"
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    out: dict = {}
    for r in rows:
        out[r[0]] = {
            "cost": float(r[1] or 0), "calls": int(r[2] or 0),
            "prompt": int(r[3] or 0), "completion": int(r[4] or 0),
            "cache_creation": int(r[5] or 0), "cache_read": int(r[6] or 0),
        }
    return out


def _daily_cost_series(from_date: str | None, to_date: str | None,
                       model_alias: str | None = None) -> list[dict]:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = "SELECT substr(ts::text,1,10) AS day, COALESCE(SUM(cost),0) FROM token_usage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day"
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    return [{"day": r[0], "cost": round(float(r[1]), 4)} for r in rows]


def _daily_token_series(from_date: str | None, to_date: str | None,
                        model_alias: str | None = None) -> list[dict]:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = ("SELECT substr(ts::text,1,10) AS day, COALESCE(SUM(prompt_tokens),0), "
           "COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
           "COALESCE(SUM(cache_creation_tokens),0) FROM token_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day"
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    return [{"day": r[0], "prompt": int(r[1]), "completion": int(r[2]),
             "cache_read": int(r[3]), "cache_creation": int(r[4])} for r in rows]


def _token_by_purpose(from_date: str | None, to_date: str | None,
                      model_alias: str | None = None) -> list[dict]:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = ("SELECT purpose, COALESCE(SUM(prompt_tokens),0) + COALESCE(SUM(completion_tokens),0) "
           "FROM token_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY purpose ORDER BY 2 DESC"
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    return [{"purpose": r[0], "tokens": int(r[1] or 0)} for r in rows]


def _cost_by_purpose(from_date: str | None, to_date: str | None,
                     model_alias: str | None = None) -> list[dict]:
    where, params = _ts_filter(from_date, to_date, model_alias)
    sql = "SELECT purpose, COALESCE(SUM(cost),0) FROM token_usage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY purpose ORDER BY 2 DESC"
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    return [{"purpose": r[0], "cost": round(float(r[1]), 4)} for r in rows]


def query_token_overview(from_date: str | None, to_date: str | None,
                         model_alias: str | None = None) -> dict:
    """概览:KPI + 成本/Tokene 趋势 + 各模块成本/Token 占比 + Top5。

    KPI 与趋势、占比均跟随所选范围与模型联动;KPI 环比对比的是
    **上一等长周期**(近30天对比再往前30天,今日对比昨日)。
    """
    from code_tutor_agent.token_usage.cost import cache_hit_rate

    if not from_date:
        from_date = _date_n_days_ago(30)
    if not to_date:
        to_date = _fmt_date(datetime.now())

    # ── KPI:当前区间 + 上一等长区间(与范围/模型联动)──
    d0 = datetime.strptime(from_date, "%Y-%m-%d")
    d1 = datetime.strptime(to_date, "%Y-%m-%d")
    duration = max((d1 - d0).days + 1, 1)
    prev_end = d0 - timedelta(days=1)
    prev_start = prev_end - timedelta(days=duration - 1)

    cur = _period_totals(from_date, to_date, model_alias)
    prev = _period_totals(_fmt_date(prev_start), _fmt_date(prev_end), model_alias)

    hit_cur = cache_hit_rate(cur)
    hit_prev = cache_hit_rate(prev)

    def _delta(a: float, b: float) -> float:
        return round((a - b) / b * 100, 1) if b else 0.0

    def _tokens(t: dict) -> float:
        return t["prompt"] + t["completion"]

    avg_day_cost = cur["cost"] / duration
    avg_prev_day_cost = prev["cost"] / duration

    kpis = [
        {"label": "总成本", "value": round(cur["cost"], 2),
         "delta": _delta(cur["cost"], prev["cost"])},
        {"label": "总调用", "value": cur["calls"],
         "delta": _delta(cur["calls"], prev["calls"])},
        {"label": "缓存命中率", "value": round(hit_cur * 100, 1),
         "delta": round((hit_cur - hit_prev) * 100, 1)},
        {"label": "预估月费", "value": round(avg_day_cost * 30, 2),
         "delta": _delta(avg_day_cost, avg_prev_day_cost)},
        {"label": "总 Token", "value": _tokens(cur),
         "delta": _delta(_tokens(cur), _tokens(prev))},
        {"label": "输入 Token", "value": cur["prompt"],
         "delta": _delta(cur["prompt"], prev["prompt"])},
        {"label": "输出 Token", "value": cur["completion"],
         "delta": _delta(cur["completion"], prev["completion"])},
        {"label": "缓存读", "value": cur["cache_read"],
         "delta": _delta(cur["cache_read"], prev["cache_read"])},
    ]

    # ── 趋势 / 占比 / Top5:随筛选范围 + 模型 ──
    trend = _daily_cost_series(from_date, to_date, model_alias)
    token_trend = _daily_token_series(from_date, to_date, model_alias)
    share = _cost_by_purpose(from_date, to_date, model_alias)
    total = sum(x["cost"] for x in share) or 1.0
    module_share = [
        {"purpose": x["purpose"], "cost": x["cost"], "pct": round(x["cost"] / total * 100, 1)}
        for x in share
    ]
    t_share = _token_by_purpose(from_date, to_date, model_alias)
    t_total = sum(x["tokens"] for x in t_share) or 1
    module_token_share = [
        {"purpose": x["purpose"], "tokens": x["tokens"], "pct": round(x["tokens"] / t_total * 100, 1)}
        for x in t_share
    ]
    top = sorted(share, key=lambda x: -x["cost"])[:5]
    return {
        "kpis": kpis, "trend": trend, "tokenTrend": token_trend,
        "moduleShare": module_share, "moduleTokenShare": module_token_share,
        "topPurposes": top, "totalCost": round(cur["cost"], 2),
        "totalCalls": cur["calls"],
        "range": {"from": from_date, "to": to_date, "model": model_alias or "全部"},
    }


def query_token_purposes(from_date: str | None, to_date: str | None,
                         model_alias: str | None = None) -> list[dict]:
    """按业务用途统计(含环比)。"""
    from code_tutor_agent.token_usage.cost import cache_hit_rate, category_of

    if not from_date:
        from_date = _date_n_days_ago(30)
    if not to_date:
        to_date = _fmt_date(datetime.now())

    cur = _purpose_totals(from_date, to_date, model_alias)
    days = _days_between(from_date, to_date)
    pf, pt = _shift_period(from_date, to_date, days)
    prev = _purpose_totals(pf, pt, model_alias)

    rows = []
    for p, agg in cur.items():
        rec = {
            "prompt_tokens": agg["prompt"], "completion_tokens": agg["completion"],
            "cache_creation_tokens": agg["cache_creation"], "cache_read_tokens": agg["cache_read"],
        }
        hit = round(cache_hit_rate(rec) * 100, 1)
        prev_cost = prev.get(p, {}).get("cost", 0.0) or 0.0
        delta = round((agg["cost"] - prev_cost) / prev_cost * 100, 1) if prev_cost else 0.0
        rows.append({
            "purpose": p, "category": category_of(p), "calls": agg["calls"],
            "promptK": round(agg["prompt"] / 1000, 1), "completionK": round(agg["completion"] / 1000, 1),
            "cacheReadK": round(agg["cache_read"] / 1000, 1), "hit": hit,
            "cost": round(agg["cost"], 2), "delta": delta,
        })
    rows.sort(key=lambda x: -x["cost"])
    return rows


def query_token_cache(from_date: str | None, to_date: str | None,
                      model_alias: str | None = None) -> list[dict]:
    """各用途缓存命中率 + 失效诊断。"""
    from code_tutor_agent.token_usage.cost import cache_hit_rate, category_of

    if not from_date:
        from_date = _date_n_days_ago(30)
    if not to_date:
        to_date = _fmt_date(datetime.now())

    cur = _purpose_totals(from_date, to_date, model_alias)
    rows = []
    for p, agg in cur.items():
        rec = {
            "prompt_tokens": agg["prompt"], "completion_tokens": agg["completion"],
            "cache_creation_tokens": agg["cache_creation"], "cache_read_tokens": agg["cache_read"],
        }
        hit = round(cache_hit_rate(rec) * 100, 1)
        tip = None
        if hit < 40:
            tip = (
                "system prompt 过短且用户对话前置，前缀不稳定 → 把固定评分 rubric / 抽取 schema 移到 prompt 最前"
                if p == "memory-extract" else
                "判题 system prompt 含动态题目正文，建议拆为「固定评分规则(前)+题目(后)」两截"
                if p == "judge" else
                "工具调用链中用户代码 / 执行结果置于 prompt 前部，固定 tool schema 与规则应前置、动态输入后置"
                if p == "tutor-eval" else
                "编辑轨迹把用户代码 / diff 变化量放在最前，固定追踪规则应前置、动态轨迹内容后置"
                if p == "edit-trace" else
                "前缀不稳定，建议将固定内容(system prompt / 规则)前置、动态内容(用户代码 / 题目 / 对话)后置"
            )
        rows.append({"purpose": p, "category": category_of(p), "hit": hit, "tip": tip})
    rows.sort(key=lambda x: -x["hit"])
    return rows


def query_token_budget() -> dict:
    """预算使用 + 预警事件(单用户:平台日 + 用户日 + 单 Session 三层)。"""
    from code_tutor_agent.config import get_token_daily_budget, get_token_session_budget, get_token_user_daily_budget

    today = _fmt_date(datetime.now())
    today_tot = _period_totals(today, today)
    used = today_tot["cost"]
    daily_limit = get_token_daily_budget()
    user_daily_limit = get_token_user_daily_budget()

    row = _with_conn(lambda c: c.execute(
        "SELECT session_id, COALESCE(SUM(cost),0) FROM token_usage WHERE ts >= ? "
        "GROUP BY session_id ORDER BY 2 DESC LIMIT 1",
        (today,),
    ).fetchone())
    max_sid = row[0] if row else ""
    max_session = float(row[1]) if row else 0.0
    session_limit = get_token_session_budget()

    budgets = [
        {"name": "你的日预算（总额）", "used": round(used, 2), "limit": daily_limit},
        {"name": "用户日预算（每人）", "used": round(used, 2), "limit": user_daily_limit},
        {"name": "单 Session 预算", "used": round(max_session, 2), "limit": session_limit},
    ]

    cache_rows = query_token_cache(today, today)
    cur = _purpose_totals(today, today)
    pf, pt = _shift_period(today, today, 1)
    prev = _purpose_totals(pf, pt)

    alerts = []
    if daily_limit and used >= 0.9 * daily_limit:
        alerts.append({
            "level": "error",
            "title": f"今日成本已用 {round(used / daily_limit * 100)}% 日预算",
            "detail": "阈值超限后将告警并在 get_llm 入口熔断，出题降级至 static_pool",
        })
    if max_sid and session_limit and max_session >= 0.9 * session_limit:
        alerts.append({
            "level": "error",
            "title": f"Session {max_sid} 累计 ¥{max_session:.1f} / ¥{session_limit:.1f}（{round(max_session / session_limit * 100)}%）",
            "detail": "下次出题将触发降级至 static_pool",
        })
    for r in cache_rows:
        if r["hit"] < 40:
            alerts.append({
                "level": "warn",
                "title": f"{r['purpose']} 缓存命中率仅 {r['hit']}%",
                "detail": "前缀重排后预计节省输入成本",
            })
    for p, agg in cur.items():
        prev_cost = prev.get(p, {}).get("cost", 0.0) or 0.0
        if prev_cost and agg["cost"] >= prev_cost * 1.1:
            d = round((agg["cost"] - prev_cost) / prev_cost * 100, 1)
            alerts.append({
                "level": "warn",
                "title": f"{p} 用途成本环比 +{d}%",
                "detail": "检查 max_tokens / temperature 是否可下调",
            })
    return {"budgets": budgets, "alerts": alerts}


def query_token_usage_recent(limit: int = 100, from_date: str | None = None,
                              to_date: str | None = None) -> list[dict]:
    """调用明细(token_usage 明细表),按时间倒序。"""
    where, params = _ts_filter(from_date, to_date)
    sql = ("SELECT ts, session_id, purpose, model_alias, prompt_tokens, completion_tokens, "
           "cache_read_tokens, cost, latency_ms FROM token_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    rows = _with_conn(lambda c: c.execute(sql, params).fetchall())
    return [{
        "ts": r[0], "session_id": r[1], "purpose": r[2], "model_alias": r[3],
        "prompt_tokens": r[4], "completion_tokens": r[5], "cache_read_tokens": r[6],
        "cost": round(float(r[7]), 4), "latency_ms": r[8],
    } for r in rows]


def export_token_usage_csv(from_date: str | None = None, to_date: str | None = None,
                           limit: int = 5000) -> str:
    """导出调用明细为 CSV 字符串。"""
    import csv
    import io

    rows = query_token_usage_recent(limit=limit, from_date=from_date, to_date=to_date)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "session_id", "purpose", "model_alias",
                "prompt_tokens", "completion_tokens", "cache_read_tokens", "cost", "latency_ms"])
    for r in rows:
        w.writerow([r["ts"], r["session_id"], r["purpose"], r["model_alias"],
                    r["prompt_tokens"], r["completion_tokens"], r["cache_read_tokens"],
                    r["cost"], r["latency_ms"]])
    return buf.getvalue()


# ── 用户设置（自定义 LLM 接入，settings 路由使用）──

def get_user_settings(user_id: int) -> dict | None:
    """读取用户设置行；无记录返回 None（视为全部走服务器默认）。"""
    rows = _with_conn(
        lambda c: c.execute(
            "SELECT user_id, llm_mode, llm_model, llm_base_url, llm_api_key, updated_at "
            "FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchall()
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "user_id": r[0], "llm_mode": r[1], "llm_model": r[2],
        "llm_base_url": r[3], "llm_api_key": r[4], "updated_at": r[5],
    }


def save_user_settings(user_id: int, mode: str, model: str, base_url: str, api_key: str) -> None:
    """UPSERT 用户设置（llm_mode='custom' 时由调用方保证三项非空）。"""
    _with_conn(
        lambda c: c.execute(
            """
            INSERT INTO user_settings (user_id, llm_mode, llm_model, llm_base_url, llm_api_key)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                llm_mode = excluded.llm_mode,
                llm_model = excluded.llm_model,
                llm_base_url = excluded.llm_base_url,
                llm_api_key = excluded.llm_api_key,
                updated_at = LOCALTIMESTAMP
            """,
            (user_id, mode, model, base_url, api_key),
        )
    )


def delete_user_settings(user_id: int) -> None:
    """删除用户设置（切回服务器默认模式时调用）。"""
    _with_conn(lambda c: c.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,)))
