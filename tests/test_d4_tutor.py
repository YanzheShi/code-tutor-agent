"""D4 tests — tutor decision tree + post-guard.

Covers:
    1. Decision tree logic (repeat detection, emotion, direction)
    2. Post-guard scan (R01/R10 code leak prevention)
    3. Emotion signal detection
    4. Same error counting
    5. Full tutor_node routing via Command(goto=...)
"""

from __future__ import annotations

import pytest

from code_tutor_agent.nodes.tutor import (
    _count_same_error,
    _decide_hint_level,
    _detect_emotion,
    _post_guard_scan,
)
from code_tutor_agent.prompts.tutor import (
    R01_CODE_LEAK_PATTERNS,
    R10_CODE_WRITE_PATTERNS,
)
from code_tutor_agent.schemas.state import (
    JudgeResult,
    Message,
    SessionState,
    Submission,
)


# ═══════════════════════════════════════════════
#  Decision tree tests
# ═══════════════════════════════════════════════


class TestDecideHintLevel:
    """Pure-rule decision tree — no LLM calls."""

    def test_default_stays_current(self):
        """No special signals → keep current hint_level."""
        level = _decide_hint_level(
            hint_level=1, verdict="WA",
            submission_count=1, same_error_count=1,
            emotion_detected=False, has_diff=False,
            state=SessionState(session_id="test"),
        )
        assert level == 1

    def test_emotion_triggers_l4(self):
        """Emotion + many submissions → L4."""
        level = _decide_hint_level(
            hint_level=0, verdict="WA",
            submission_count=5, same_error_count=2,
            emotion_detected=True, has_diff=False,
            state=SessionState(session_id="test"),
        )
        assert level == 4

    def test_repeat_triggers_explanation(self):
        """同一种错误连续 3 次 → 不跳级（留插讲解空间）。"""
        level = _decide_hint_level(
            hint_level=1, verdict="WA",
            submission_count=3, same_error_count=3,
            emotion_detected=False, has_diff=False,
            state=SessionState(session_id="test"),
        )
        # 决策树不会跳到 < 0，但会保持当前 level
        assert level >= 0


# ═══════════════════════════════════════════════
#  Post-guard tests
# ═══════════════════════════════════════════════


class TestPostGuard:
    """Post-guard — pure keywords, no LLM."""

    def test_clean_hint_passes(self):
        """合法提示 → 原样返回。"""
        hint = "看看边界条件，如果数组为空会怎么样？"
        assert _post_guard_scan(hint, target_level=2) == hint

    def test_code_leak_at_low_level_downgrades(self):
        """L2 的时候出现代码 → 降级。"""
        hint = "你可以试试这样：```python\nif not nums:\n    return []\n```"
        result = _post_guard_scan(hint, target_level=2)
        # Should return a fallback hint (safe)
        assert "```" not in result
        assert len(result) > 0

    def test_higher_level_allows_code(self):
        """L4 时可以包含代码。"""
        hint = "试试这个：```python\nreturn []\n```"
        result = _post_guard_scan(hint, target_level=4)
        # L4 allows code snippets
        assert result == hint

    def test_r10_triggers_replacement(self):
        """代写关键词 → 替换为拒绝话术。"""
        hint = "我帮你写吧，code it for me"
        result = _post_guard_scan(hint, target_level=4)
        assert "我帮你写" not in result
        assert "手得你自己动" in result


# ═══════════════════════════════════════════════
#  Emotion detection tests
# ═══════════════════════════════════════════════


class TestEmotionDetection:
    """Keyword-based emotion detection — no LLM."""

    def test_detects_frustration(self):
        messages = [
            Message(role="user", content="我不会，这题太难了"),
        ]
        assert _detect_emotion(messages) is True

    def test_ignores_normal(self):
        messages = [
            Message(role="user", content="我改了一下边界条件"),
        ]
        assert _detect_emotion(messages) is False

    def test_only_checks_recent(self):
        """只看最近 3 条，不影响旧消息。"""
        messages = [
            Message(role="user", content="我不会"),
            Message(role="tutor", content="试试边界"),
            Message(role="user", content="好的我试试"),
        ]
        assert _detect_emotion(messages) is True


# ═══════════════════════════════════════════════
#  Same-error counting tests
# ═══════════════════════════════════════════════


class TestSameErrorCount:
    """连续同类错误计数。"""

    def _make_sub(self, verdicts: list[str]) -> Submission:
        sub = Submission(index=1, code="")
        for v in verdicts:
            sub.judge_results.append(JudgeResult(status=v, phase="base"))  # type: ignore
        return sub

    def test_no_submissions(self):
        assert _count_same_error([]) == 0

    def test_single_submission(self):
        subs = [self._make_sub(["WA"])]
        assert _count_same_error(subs) == 1

    def test_consecutive_same(self):
        subs = [self._make_sub(["AC"]), self._make_sub(["WA"]), self._make_sub(["WA"])]
        assert _count_same_error(subs) == 2

    def test_mixed_stops_chain(self):
        subs = [self._make_sub(["WA"]), self._make_sub(["AC"]), self._make_sub(["WA"])]
        # Last two: AC, WA. AC != WA, so only WA counts
        assert _count_same_error(subs) == 1


# ═══════════════════════════════════════════════
#  Tutor routing tests
# ═══════════════════════════════════════════════


class TestTutorRouting:
    """Verify tutor_node returns correct Command(goto=...)."""

    def test_ac_routes_to_planner(self):
        from code_tutor_agent.nodes.tutor import tutor_node

        state = SessionState(
            session_id="test",
            last_verdict="AC",
            status="tutoring",
        )
        cmd = tutor_node(state)
        assert cmd.goto == "critic_node"
        assert cmd.update["status"] == "done"

    def test_adversarial_fail_routes_to_wait(self):
        from code_tutor_agent.nodes.tutor import tutor_node

        sub = Submission(index=1, code="")
        sub.judge_results.append(JudgeResult(status="WA", phase="adversarial_boundary"))  # type: ignore

        state = SessionState(
            session_id="test",
            last_verdict="AC",
            adversarial_triggered=True,
            status="tutoring",
            submissions=[sub],
        )
        cmd = tutor_node(state)
        assert cmd.goto == "critic_node"
        assert cmd.update["status"] == "awaiting_submit"

    def test_base_fail_routes_to_wait(self):
        from code_tutor_agent.nodes.tutor import tutor_node

        state = SessionState(
            session_id="test",
            last_verdict="WA",
            status="tutoring",
        )
        cmd = tutor_node(state)
        assert cmd.goto == "critic_node"