"""
Verify: /admin/profile now returns ac_rate computed from submissions table.
- Fresh DB (no submissions): ac_rate should be 0.0
- DB with AC/WA/TLE/RE: ac_rate should match real stats
"""
import json
import pytest
import sqlite3
from fastapi.testclient import TestClient
from code_tutor_agent.api.main import app
from code_tutor_agent.db.database import init_db, get_profile


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_profile_has_ac_rate_key(client):
    """AC rate key must be present in the response."""
    resp = client.get("/admin/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "ac_rate" in data, "ac_rate key missing from profile response"


def test_ac_rate_matches_submissions_count():
    """ac_rate in profile should equal AC count / total count * 100 from submissions table."""
    conn = sqlite3.connect("data/db/code_tutor.db")
    sub_rows = conn.execute("SELECT verdict FROM submissions").fetchall()
    total = len(sub_rows)
    ac = sum(1 for r in sub_rows if r[0] == "AC")
    expected = round(ac / total * 100, 1) if total else 0.0
    conn.close()

    result = get_profile()
    assert result.ac_rate == expected, (
        f"ac_rate mismatch: expected={expected}, got={result.ac_rate}, "
        f"submissions_total={total}, ac={ac}"
    )


def test_ac_rate_zero_when_no_submissions(tmp_path, monkeypatch):
    """If submissions table is empty, ac_rate should be 0.0."""
    temp_db = str(tmp_path / "test_code_tutor.db")
    monkeypatch.setattr("code_tutor_agent.db.database.DB_PATH", temp_db)
    init_db()

    result = get_profile()
    assert result.ac_rate == 0.0, f"Expected 0.0 with no submissions, got {result.ac_rate}"


def test_ac_rate_nonzero_with_ac_submissions(tmp_path, monkeypatch):
    """ac_rate should be > 0 when there are AC submissions."""
    temp_db = str(tmp_path / "test_code_tutor.db")
    monkeypatch.setattr("code_tutor_agent.db.database.DB_PATH", temp_db)
    init_db()

    # Insert some submissions manually
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO submissions (problem_id, student_code, status, verdict) VALUES (1, 'print(1)', 'judged', 'AC')")
    conn.execute("INSERT INTO submissions (problem_id, student_code, status, verdict) VALUES (1, 'print(2)', 'judged', 'AC')")
    conn.execute("INSERT INTO submissions (problem_id, student_code, status, verdict) VALUES (1, 'print(3)', 'judged', 'WA')")
    conn.commit()
    conn.close()

    # Insert a profile row first so get_profile computes ac_rate instead of returning defaults
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO profiles (user_id, profile_json) VALUES "
        "('default', '{\"proficiency\":0.5,\"stability\":0.5,\"forget_days\":0,\"common_errors\":[],\"attempts\":0,\"error_modes\":{}}')"
    )
    conn.commit()
    conn.close()

    result = get_profile()
    expected = round(2 / 3 * 100, 1)  # ~66.7
    assert result.ac_rate == expected, f"Expected {expected}, got {result.ac_rate}"
