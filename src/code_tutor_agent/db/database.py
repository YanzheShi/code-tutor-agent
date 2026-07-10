"""SQLite database module for persistent problem storage."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Optional

from .models import DBProblem, DBSubmission

logger = logging.getLogger(__name__)


# 数据库文件位于项目根目录 /data/db/
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "db", "code_tutor.db")


def _get_conn() -> sqlite3.Connection:
    """Create a new SQLite connection with WAL mode and Row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _with_conn(fn):
    """Execute *fn(cursor)* inside a try/commit/except/rollback/finally block.

    Ensures the connection is always closed, even on error.
    """
    conn = _get_conn()
    try:
        result = fn(conn.cursor())
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they do not exist yet (D2 schema)."""
    logger.info("▶ init_db()")
    try:
        _with_conn(lambda cursor: _init_db_tables(cursor))
    except Exception as exc:
        logger.error("init_db() failed: %s", exc)
        raise


def _init_db_tables(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for col_sql in [
    "ALTER TABLE problems ADD COLUMN starter_code TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE problems ADD COLUMN visible_test_cases_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE problems ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'",
        "ALTER TABLE problems ADD COLUMN source_url TEXT DEFAULT ''",
        "ALTER TABLE problems ADD COLUMN alternative_solutions TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE problems ADD COLUMN function_signature TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE problems ADD COLUMN constraints_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE submissions ADD COLUMN verdict TEXT DEFAULT ''",
        "ALTER TABLE submissions ADD COLUMN judge_results TEXT DEFAULT '[]'",
    ]:
        try:
            cursor.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        cursor.execute("ALTER TABLE problems DROP COLUMN solution")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id INTEGER NOT NULL,
            student_code TEXT NOT NULL,
            status TEXT NOT NULL,
            verdict TEXT DEFAULT '',
            judge_results TEXT DEFAULT '[]',
            feedback TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (problem_id) REFERENCES problems (id)
        )
    """)


# ── helpers ──


def _row_to_db_problem(row: sqlite3.Row) -> DBProblem:
    """Convert a SQLite Row (from SELECT * on problems) to DBProblem."""
    data = dict(row)
    return DBProblem(**data)


def _row_to_db_submission(row: sqlite3.Row) -> DBSubmission:
    """Convert a SQLite Row (from SELECT on submissions) to DBSubmission."""
    data = dict(row)
    # Alias: frontend expects 'timestamp' but DB stores 'created_at'
    if "created_at" in data and "timestamp" not in data:
        data["timestamp"] = data.pop("created_at")
    return DBSubmission(**data)


# ── save ──


def save_problem(problem_dict: dict) -> int:
    """Save a problem to the database. Returns the existing or new problem ID.

    Args:
        problem_dict: Dict with keys matching the logical problem schema
            (title, topic, difficulty, description, test_cases, etc.).
            Accepts both camelCase (test_cases) and snake_case (test_cases_json) keys.

    Returns:
        The problem ID (existing if dedup'd, new otherwise).
    """
    logger.info("▶ save_problem()")
    init_db()
    try:
        return _with_conn(lambda cursor: _save_problem(cursor, problem_dict))
    except Exception as exc:
        logger.error("save_problem() failed for '%s': %s", problem_dict.get("title", "?"), exc)
        raise


def _save_problem(cursor, problem_dict: dict) -> int:
    title = problem_dict.get("title", "")
    if not title:
        raise ValueError("save_problem() requires a 'title'")

    cursor.execute("SELECT id FROM problems WHERE title = ?", (title,))
    existing = cursor.fetchone()
    if existing:
        logger.info("Problem '%s' already exists (id=%d), skipping insert", title, existing["id"])
        return existing["id"]

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

    cursor.execute("""
        INSERT INTO problems
            (title, topic, difficulty, description, test_cases_json, visible_test_cases_json,
             optimal_solution, brute_solution, function_signature, adversarial_spec_json,
             time_complexity, space_complexity, novelty_score, starter_code,
             source, source_url, alternative_solutions, constraints_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        problem_dict.get("source", "generated"),
        problem_dict.get("source_url", ""),
        alt,
        constraints,
    ))
    problem_id = cursor.lastrowid
    logger.info("save_problem() — id=%d, title=%s", problem_id, title)
    return problem_id


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


def update_problem_test_cases(problem_id: int, test_cases: list[dict]) -> None:
    """Update test cases for an existing problem (Day2 background generation).

    Only updates test_cases_json — visible_test_cases_json is left untouched
    to preserve the original sample/visible test cases set during import.
    """
    try:
        _with_conn(lambda cursor: cursor.execute(
            "UPDATE problems SET test_cases_json = ? WHERE id = ?",
            (json.dumps(test_cases, ensure_ascii=False), problem_id),
        ))
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


def save_submission(problem_id: int, code: str, verdict: str, judge_results: list[dict]) -> int:
    """Save a submission record to the database. Returns the submission ID."""
    try:
        def _do(cursor):
                    cursor.execute(
                        "INSERT INTO submissions (problem_id, student_code, status, verdict, judge_results) "
                        "VALUES (?, ?, 'judged', ?, ?)",
                        (problem_id, code, verdict, json.dumps(judge_results, ensure_ascii=False)),
                    )
                    return cursor.lastrowid
        sub_id = _with_conn(_do)
        logger.info("save_submission() — id=%d, problem=%d, verdict=%s", sub_id, problem_id, verdict)
        return sub_id
    except Exception as exc:
        logger.error("Failed to save submission for problem %d: %s", problem_id, exc)
        raise


def get_submissions_by_problem(problem_id: int, limit: int = 50) -> list[dict]:
    """Return recent submissions for a problem.

    Returns list of dicts (via DBSubmission.to_dict()) for frontend compatibility.
    """
    try:
        rows = _with_conn(lambda cursor: cursor.execute(
            "SELECT id, problem_id, student_code, verdict, judge_results, status, created_at "
            "FROM submissions WHERE problem_id = ? ORDER BY id DESC LIMIT ?",
            (problem_id, limit),
        ).fetchall())

        result = []
        for row in rows:
            sub = DBSubmission(**dict(row))
            result.append(sub.to_dict())
        logger.info("get_submissions_by_problem() — problem=%d, %d rows", problem_id, len(result))
        return result
    except Exception as exc:
        logger.error("get_submissions_by_problem(%d) failed: %s", problem_id, exc)
        raise


# ── User profile ──

# ── User profile ──


def get_profile(user_id: str = "default"):
    """Read the user profile from the profiles table.
    Returns a dict with default values if no row exists yet.
    """
    import json as _json
    from .models import DBProfile
    try:
        row = _with_conn(lambda cursor: cursor.execute(
            "SELECT profile_json FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone())

        if not row:
            logger.info("get_profile() — no profile for '%s', returning defaults", user_id)
            return DBProfile()

        data = _json.loads(row["profile_json"])
        return DBProfile(**data)
    except Exception as exc:
        logger.error("get_profile(%s) failed: %s", user_id, exc)
        return DBProfile()


def save_profile(profile, user_id: str = "default"):
    """Save (upsert) a user profile to the profiles table."""
    import json as _json
    try:
        _with_conn(lambda cursor: cursor.execute(
            "INSERT INTO profiles (user_id, profile_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET profile_json = excluded.profile_json, updated_at = CURRENT_TIMESTAMP",
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
