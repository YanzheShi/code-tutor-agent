"""Test: profile scoring — pure function, no IO."""
from __future__ import annotations

from code_tutor_agent.profile.scoring import apply_delta, ELO_INIT, ELO_MIN, ELO_MAX


def _empty_profile():
    return {
        "prof": {},
        "prof_elo_raw": {},
        "stab": {},
        "forget": {},
        "errors": {"_global": {}, "per_tag": {}},
        "attempts": {},
        "meta": {},
    }


def _delta(tag="array_two_pointers", outcome="AC", prob_elo=1500, fps=None):
    return {
        "tag_primary": tag,
        "prob_elo": prob_elo,
        "outcome": outcome,
        "fingerprints": fps or [],
        "misunderstanding_level": None,
    }


class TestNewTag:
    """新 tag 首次出现的行为验证。"""

    def test_first_ac_elo_between_0_and_0_5(self):
        profile = _empty_profile()
        out = apply_delta(profile, _delta(), problem_id=1, code_hash="abc", now=1_700_000_000)
        prof = out["prof"]["array_two_pointers"]
        assert 0 < prof < 0.5, f"Expected prof in (0, 0.5), got {prof}"

    def test_first_ac_stab_window_has_one(self):
        out = apply_delta(_empty_profile(), _delta(), problem_id=1, code_hash="abc", now=1_700_000_000)
        assert out["stab"]["array_two_pointers"]["window"] == [1]

    def test_first_ac_attempts_count_is_1(self):
        out = apply_delta(_empty_profile(), _delta(), problem_id=1, code_hash="abc", now=1_700_000_000)
        assert out["attempts"][1]["count"] == 1
        assert out["attempts"][1]["last_status"] == "AC"

    def test_first_wa_elo_decreases(self):
        out = apply_delta(_empty_profile(), _delta(outcome="WA"), problem_id=1, code_hash="abc", now=1_700_000_000)
        prof = out["prof"]["array_two_pointers"]
        assert prof < 0.17, f"Expected prof < 0.17 after WA, got {prof}"


class TestMultiTag:
    """多 tag 交互验证。"""

    def test_second_tag_untouched_by_first(self):
        d1 = _delta(tag="array_two_pointers", outcome="AC")
        p1 = apply_delta(_empty_profile(), d1, problem_id=1, code_hash="abc", now=1_700_000_000)
        d2 = _delta(tag="dp_1d", outcome="WA")
        p2 = apply_delta(p1, d2, problem_id=2, code_hash="def", now=1_700_000_100)
        # array 的 prof 不应被 dp 影响
        assert p2["prof"]["array_two_pointers"] == p1["prof"]["array_two_pointers"]
        # dp 的 prof 应该 < 0.17
        assert p2["prof"]["dp_1d"] < 0.17

    def test_forget_decay_other_tags(self):
        d1 = _delta(tag="array_two_pointers", outcome="AC")
        p1 = apply_delta(_empty_profile(), d1, problem_id=1, code_hash="abc", now=0)
        # 一周后练 dp
        d2 = _delta(tag="dp_1d", outcome="AC")
        one_week = 7 * 24 * 3600
        p2 = apply_delta(p1, d2, problem_id=2, code_hash="def", now=one_week)
        # array 的 decay 应该接近 0
        assert p2["forget"]["array_two_pointers"]["decay"] < 0.1, "Expected near-zero decay after a week"


class TestStabWindow:
    """稳定性滑动窗验证。"""

    def test_window_capped_at_10(self):
        profile = _empty_profile()
        for i in range(15):
            outcome = "AC" if i % 2 == 0 else "WA"
            profile = apply_delta(profile, _delta(outcome=outcome), problem_id=1, code_hash="x", now=float(i))
        assert len(profile["stab"]["array_two_pointers"]["window"]) == 10
        assert profile["stab"]["array_two_pointers"]["variance"] > 0

    def test_all_ac_zero_variance(self):
        profile = _empty_profile()
        for i in range(5):
            profile = apply_delta(profile, _delta(outcome="AC"), problem_id=1, code_hash="x", now=float(i))
        assert profile["stab"]["array_two_pointers"]["variance"] == 0.0


class TestErrors:
    """错误指纹验证。"""

    def test_fingerprint_recorded(self):
        profile = _empty_profile()
        delta = _delta(tag="array_two_pointers", outcome="WA", fps=["off_by_one", "empty_input"])
        out = apply_delta(profile, delta, problem_id=1, code_hash="abc", now=1_700_000_000)
        assert out["errors"]["_global"]["off_by_one"] == 1
        assert out["errors"]["per_tag"]["array_two_pointers"]["off_by_one"] == 1
        assert out["errors"]["_global"]["empty_input"] == 1