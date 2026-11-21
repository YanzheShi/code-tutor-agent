"""随机维度文案回归（2026-11-21）。

用户："请出一道中等难度的算法题，主题随机。"
旧行为：回成 "随机方向、随机难度"，把显式的 medium 说成随机。
本用例锁住：只有真正交给 AI 的维度才写成「随机」。
"""

from __future__ import annotations

from code_tutor_agent.agents.agent_dialog import (
    build_ready_message,
    random_scope_for_message,
)


def test_topic_random_difficulty_fixed():
    assert random_scope_for_message("请出一道中等难度的算法题，主题随机") == (True, False)
    msg = build_ready_message(
        "滑动窗口", "medium", topic_random=True, difficulty_random=False,
    )
    assert "随机方向" in msg.content
    assert "中等" in msg.content
    assert "随机难度" not in msg.content


def test_topic_fixed_difficulty_random():
    assert random_scope_for_message("来一道滑动窗口的题，难度随意") == (False, True)
    msg = build_ready_message(
        "滑动窗口", "medium", topic_random=False, difficulty_random=True,
    )
    assert "滑动窗口" in msg.content
    assert "随机难度" in msg.content
    assert "随机方向" not in msg.content


def test_both_random_keeps_original_copy():
    assert random_scope_for_message("随机来一道") == (True, True)
    msg = build_ready_message(
        "滑动窗口", "medium", topic_random=True, difficulty_random=True,
    )
    assert "随机方向" in msg.content
    assert "随机难度" in msg.content


def test_both_explicit_no_random_words():
    assert random_scope_for_message("请出一道中等难度的链表题") == (False, False)
    msg = build_ready_message("链表", "medium")
    assert "链表" in msg.content
    assert "中等" in msg.content
    assert "随机" not in msg.content
