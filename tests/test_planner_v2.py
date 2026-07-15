"""Unit tests for planner v2 profile-based topic selection (Bug 1).

These tests mock ``get_user_profile_v2`` so they run without a DB or LLM.
"""
import pytest

from code_tutor_agent.nodes import planner


def _make_profile(prof: dict, forget: dict | None = None, stab: dict | None = None) -> dict:
    return {
        "prof": prof,
        "prof_elo_raw": {k: 1500.0 for k in prof},
        "stab": stab or {},
        "forget": forget or {},
        "errors": {"_global": {}, "per_tag": {}},
        "attempts": {},
        "meta": {},
    }


def test_v2_selects_weakest_tag(monkeypatch):
    prof = {"array_basics": 0.1, "linkedlist_basics": 0.9}
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: _make_profile(prof),
    )
    topic, difficulty = planner._select_topic_by_v2_profile()
    assert topic == "数组+哈希表"
    assert difficulty == "easy"  # prof < 0.3


def test_v2_empty_profile_returns_none(monkeypatch):
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: _make_profile({}),
    )
    assert planner._select_topic_by_v2_profile() is None


def test_v2_forget_boosts_weak_tag(monkeypatch):
    # Both tags equal prof, but array_basics has decayed (forgotten) more.
    prof = {"array_basics": 0.5, "linkedlist_basics": 0.5}
    forget = {
        "array_basics": {"last_seen": 0.0, "decay": 0.2},
        "linkedlist_basics": {"last_seen": 0.0, "decay": 1.0},
    }
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: _make_profile(prof, forget=forget),
    )
    topic, _ = planner._select_topic_by_v2_profile()
    assert topic == "数组+哈希表"


def test_select_topic_default_uses_v2(monkeypatch):
    prof = {"array_two_pointers": 0.2}
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: _make_profile(prof),
    )
    topic, difficulty = planner._select_topic([], "next_in_plan")
    assert topic == "双指针"
    assert difficulty == "easy"


def test_select_topic_v2_read_error_falls_back(monkeypatch):
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    # Should fall back to legacy profile-based selection without raising.
    topic, difficulty = planner._select_topic([], "next_in_plan")
    assert isinstance(topic, str) and isinstance(difficulty, str)
