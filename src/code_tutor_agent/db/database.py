"""SQLite database module for persistent problem storage."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)



# Database file lives at project root /data/db/
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "db", "code_tutor.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they do not exist yet (D2 schema)."""
    logger.info("▶ init_db()")
    conn = _get_conn()
    cursor = conn.cursor()

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

    # -- Migration: add columns that may be missing in existing DBs --
    for col_sql in [
        "ALTER TABLE problems ADD COLUMN starter_code TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE problems ADD COLUMN visible_test_cases_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE problems ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'",
        "ALTER TABLE problems ADD COLUMN source_url TEXT DEFAULT ''",
        "ALTER TABLE problems ADD COLUMN alternative_solutions TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE submissions ADD COLUMN verdict TEXT DEFAULT ''",
        "ALTER TABLE submissions ADD COLUMN judge_results TEXT DEFAULT '[]'",
    ]:
        try:
            cursor.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    # -- Migration: drop redundant solution column (SQLite 3.35+) --
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

    conn.commit()
    conn.close()


def save_problem(problem_dict: dict) -> int:
    """Save a problem to the database. Returns the existing or new problem ID."""
    logger.info("▶ save_problem()")
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()

    # ── Dedup: check if a problem with this title already exists ──
    cursor.execute("SELECT id FROM problems WHERE title = ?", (problem_dict["title"],))
    existing = cursor.fetchone()
    if existing:
        logger.info("Problem '%s' already exists (id=%d), skipping insert", problem_dict["title"], existing["id"])
        conn.close()
        return existing["id"]

    if "alternative_solutions" not in problem_dict:
        alt = problem_dict.get("alternative_solutions", [])
    else:
        alt = problem_dict["alternative_solutions"]
    if not isinstance(alt, str):
        alt = json.dumps(alt, ensure_ascii=False)

    cursor.execute("""
            INSERT INTO problems
                (title, topic, difficulty, description, test_cases_json, visible_test_cases_json,
                 optimal_solution, brute_solution, adversarial_spec_json,
                 time_complexity, space_complexity, novelty_score, starter_code,
                 source, source_url, alternative_solutions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            problem_dict["title"],
            problem_dict["topic"],
            problem_dict["difficulty"],
            problem_dict["description"],
            json.dumps(problem_dict.get("test_cases", []), ensure_ascii=False),
            json.dumps(problem_dict.get("visible_test_cases", problem_dict.get("test_cases", [])), ensure_ascii=False),
            problem_dict.get("optimal_solution", ""),
            problem_dict.get("brute_solution", ""),
            json.dumps(problem_dict.get("adversarial_spec"), ensure_ascii=False)
            if problem_dict.get("adversarial_spec") else "",
            problem_dict.get("time_complexity", ""),
            problem_dict.get("space_complexity", ""),
            problem_dict.get("novelty_score", 7.0),
            problem_dict.get("starter_code", ""),
            problem_dict.get("source", "generated"),
            problem_dict.get("source_url", ""),
            alt,
        ))

    problem_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return problem_id


def get_problem_by_id(problem_id: int) -> Optional[dict[str, Any]]:
    """Retrieve a problem by its ID."""
    logger.info("▶ get_problem_by_id()")
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM problems WHERE id = ?", (problem_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    result = dict(row)
    # Deserialise JSON fields
    result["test_cases"] = json.loads(result.get("test_cases_json", "[]"))
    result["visible_test_cases"] = json.loads(result.get("visible_test_cases_json", "[]"))
    if result.get("adversarial_spec_json"):
        result["adversarial_spec"] = json.loads(result["adversarial_spec_json"])
    result["alternative_solutions"] = json.loads(result.get("alternative_solutions", "[]"))
    return result


def get_all_problem_ids() -> list[int]:
    """Return all problem IDs (for pool queries)."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM problems ORDER BY id")
    ids = [r["id"] for r in cursor.fetchall()]
    conn.close()
    return ids


def update_problem_test_cases(problem_id: int, test_cases: list[dict]) -> None:
    """Update test cases for an existing problem (Day2 background generation).

    Called after background test generation completes.
    Only updates test_cases_json — visible_test_cases_json is left untouched
    to preserve the original sample/visible test cases set during import.

    Args:
        problem_id: The ID of the problem to update.
        test_cases: The full list of test cases (visible + hidden).
    """
    import json
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE problems SET test_cases_json = ? WHERE id = ?",
        (json.dumps(test_cases, ensure_ascii=False), problem_id),
    )
    conn.commit()
    conn.close()
    logger.info("update_problem_test_cases() — id=%d, %d test cases (visible_test_cases untouched)",
                problem_id, len(test_cases))

def save_submission(problem_id: int, code: str, verdict: str, judge_results: list[dict]) -> int:
    """Save a submission record to the database.

    Returns the submission ID.
    """
    import json
    from datetime import datetime
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO submissions (problem_id, student_code, status, verdict, judge_results) VALUES (?, ?, 'judged', ?, ?)",
        (problem_id, code[:1000], verdict, json.dumps(judge_results, ensure_ascii=False)),
    )
    sub_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info("save_submission() — id=%d, problem=%d, verdict=%s", sub_id, problem_id, verdict)
    return sub_id


def get_submissions_by_problem(problem_id: int, limit: int = 50) -> list[dict]:
    """Return recent submissions for a problem."""
    import json
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, student_code, verdict, judge_results, status, created_at FROM submissions WHERE problem_id = ? ORDER BY id DESC LIMIT ?",
        (problem_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["judge_results"] = json.loads(d.get("judge_results", "[]"))
        d["timestamp"] = d.pop("created_at", "")
        result.append(d)
    logger.info("get_submissions_by_problem() — problem=%d, %d rows", problem_id, len(result))
    return result