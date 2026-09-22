"""回归测试：新用户「薄弱项」假数据（2026-09-22）。

Bug：``database.get_user_profile_v2()`` 为前端展示给**全部 32 个 tag** 补零分
（``prof=0.0`` / ``forget.decay=0.0``），使「从未练习」与「练得极差」在数据层
不可区分。下游把 0 分当真实成绩，导致：

- 新用户首条欢迎语出现「我注意到你的薄弱项有：数组基础、双指针、滑动窗口」；
- 默认选题恒为「数组 easy」（32 个 tag 并列同分，稳定排序取枚举第一个）；
- 出题 hint 恒为「数组」，且 ``_profile_hint_from`` 不传 user_id → 读 default_v2 串号。

修法：``fill_missing=False``（只读真实数据）+ ``practiced`` 白名单
（判据 = ``stab[tag]["window"]`` 非空，``apply_delta`` 只在真练习时写入）。
本文件锁死这三条行为。
"""
from __future__ import annotations

import json
import types

from code_tutor_agent.agents import agent_dialog
from code_tutor_agent.nodes import generator, planner
from code_tutor_agent.profile.tags import Tag


def _zero_filled_profile(*, practiced: list[str] | None = None) -> dict:
    """复刻 fill_missing=True 下新用户读出的画像（32 个 tag 全是 0 分）。"""
    tags = Tag.all_values()
    profile: dict = {
        "prof": {t: 0.0 for t in tags},
        "prof_elo_raw": {t: 0.0 for t in tags},
        "stab": {t: {"window": [], "variance": 0.0} for t in tags},
        "forget": {t: {"last_seen": 0.0, "decay": 1.0} for t in tags},
        "tag_names": {},
    }
    if practiced is not None:
        profile["practiced"] = practiced
    return profile


def _patch_v2(monkeypatch, profile: dict) -> None:
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_user_profile_v2",
        lambda *a, **k: profile,
    )


def _patch_legacy(monkeypatch, attempts: int = 0) -> None:
    monkeypatch.setattr(
        "code_tutor_agent.db.database.get_profile",
        lambda *a, **k: types.SimpleNamespace(attempts=attempts, proficiency=0.0),
    )


# ── 对话画像摘要 / 欢迎语 ──


def test_new_user_has_no_weakness_summary(monkeypatch):
    """新用户（零练习）不得产出任何弱项摘要 → 欢迎语走通用引导。"""
    _patch_v2(monkeypatch, _zero_filled_profile(practiced=[]))
    _patch_legacy(monkeypatch, attempts=0)

    assert agent_dialog._build_profile_summary("1") == ""


def test_zero_filled_tags_are_not_weaknesses(monkeypatch):
    """双保险：即使上游返回补零画像（prof 全 0）也不得产出弱项。

    修 bug 前这里会返回 64 条命中（32 × ``p<0.3`` + 32 × ``decay<0.5``），
    去重取前 5 = 数组基础/双指针/滑动窗口/二分查找/前缀和。
    """
    _patch_v2(monkeypatch, _zero_filled_profile(practiced=[]))
    _patch_legacy(monkeypatch, attempts=0)

    out = agent_dialog._build_profile_summary("1")
    assert out == ""
    assert "数组基础" not in out
    assert "滑动窗口" not in out


def test_practiced_weak_tag_still_reported(monkeypatch):
    """修完不能矫枉过正：真练过且分数低的 tag 仍要被报出来。"""
    profile = _zero_filled_profile(practiced=["array_two_pointers"])
    profile["prof"]["array_two_pointers"] = 0.12
    profile["stab"]["array_two_pointers"] = {"window": [0], "variance": 0.0}
    _patch_v2(monkeypatch, profile)
    _patch_legacy(monkeypatch, attempts=0)

    out = agent_dialog._build_profile_summary("1")
    assert "双指针" in out
    assert "数组基础" not in out  # 未练过的 tag 不得混进来
    assert "链表" not in out


# ── 出题 hint（generator）──


def test_new_user_profile_hint_is_none(monkeypatch):
    """原实现：32 个 0 分并列，min() 取枚举第一个 array_basics → hint 恒为「数组」。"""
    _patch_v2(monkeypatch, _zero_filled_profile(practiced=[]))
    state = types.SimpleNamespace(user_id="7")

    assert generator._profile_hint_from(state) is None


def test_profile_hint_reads_current_user_profile(monkeypatch):
    """原实现不传 user_id → 恒读 default_v2（所有用户共用一份画像）。"""
    seen: dict = {}

    def _fake(user_id: str = "default_v2", **kw):
        seen["key"] = user_id
        seen["fill_missing"] = kw.get("fill_missing")
        return {
            "prof": {"array_basics": 0.1},
            "stab": {"array_basics": {"window": [0], "variance": 0.0}},
            "forget": {},
            "practiced": ["array_basics"],
        }

    monkeypatch.setattr("code_tutor_agent.db.database.get_user_profile_v2", _fake)
    state = types.SimpleNamespace(user_id="42")

    assert generator._profile_hint_from(state) == "数组"
    assert seen["key"] == "42_v2"
    assert seen["fill_missing"] is False


# ── 默认选题（planner）──


def test_planner_ignores_zero_filled_tags(monkeypatch):
    """新用户不得被「全 0 并列」推去出题 → 返回 None 交给旧逻辑兜底。"""
    _patch_v2(monkeypatch, _zero_filled_profile(practiced=[]))

    assert planner._select_topic_by_v2_profile("1_v2") is None


# ── 数据层：fill_missing 开关 ──


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return self._row


def _patch_db_row(monkeypatch, row):
    from code_tutor_agent.db import database

    monkeypatch.setattr(database, "_with_conn", lambda fn: fn(_FakeCursor(row)))
    return database


def test_fill_missing_flag_controls_zero_filling(monkeypatch):
    database = _patch_db_row(monkeypatch, None)  # 库里没有该用户的画像记录

    filled = database.get_user_profile_v2("newbie_v2")
    assert len(filled["prof"]) == len(Tag.all_values())
    assert filled["practiced"] == []

    lean = database.get_user_profile_v2("newbie_v2", fill_missing=False)
    assert lean["prof"] == {}
    assert lean["stab"] == {}
    assert lean["forget"] == {}
    assert lean["practiced"] == []
    assert lean["tag_names"]  # 中文名映射两种模式都附带


def test_practiced_derived_from_stab_window(monkeypatch):
    stored = {
        "prof": {"array_basics": 0.2},
        "prof_elo_raw": {"array_basics": 1510.0},
        "stab": {"array_basics": {"window": [1], "variance": 0.0}},
        "forget": {"array_basics": {"last_seen": 1.0, "decay": 1.0}},
    }
    database = _patch_db_row(monkeypatch, {"profile_json": json.dumps(stored)})

    out = database.get_user_profile_v2("u_v2")
    assert out["practiced"] == ["array_basics"]
    # 补零只作用于「库里没有的 tag」，decay 默认 1.0（= 没遗忘）——
    # 旧值 0.0 会被下游读成「遗忘到极致」，与 scoring.apply_delta 首次练习写入的 1.0 矛盾。
    assert out["forget"]["dp_1d"] == {"last_seen": 0.0, "decay": 1.0}
    assert out["forget"]["array_basics"]["decay"] == 1.0
