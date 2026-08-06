"""Agent Memory 单元测试 —— 覆盖 docs/agent-memory-design.md §10.1 清单。

watermark 切片 / 门控 / merge 防御 / DAO 往返与坏数据兜底 / 渲染 / 调度快照。
所有 LLM 调用均 mock,不触网。
"""
import os
import tempfile
import threading

import pytest


class TestMemory:
    @pytest.fixture(autouse=True)
    def _temp_db(self):
        """每个测试一个干净的临时 DB(profiles 表由 init_db 创建)。"""
        from code_tutor_agent.db import database as dbmod
        self._orig_path = dbmod.DB_PATH
        self._tp = os.path.join(tempfile.gettempdir(), f"test_memory_{os.getpid()}.db")
        if os.path.exists(self._tp):
            os.remove(self._tp)
        dbmod.DB_PATH = self._tp
        from code_tutor_agent.db.database import init_db
        init_db()
        yield
        dbmod.DB_PATH = self._orig_path
        if os.path.exists(self._tp):
            os.remove(self._tp)

    # ── watermark 三规则 ──

    def test_watermark_same_session(self):
        from code_tutor_agent.memory import _delta_start, empty_memory
        mem = empty_memory()
        mem["meta"]["watermark"] = {"session_id": "s1", "count": 5}
        assert _delta_start(mem, "s1", 8) == 5

    def test_watermark_new_session(self):
        from code_tutor_agent.memory import _delta_start, empty_memory
        mem = empty_memory()
        mem["meta"]["watermark"] = {"session_id": "s1", "count": 5}
        assert _delta_start(mem, "s2", 8) == 0

    def test_watermark_shortened_reset(self):
        """普通模式换题清空 tutor_messages → 消息数变短 → 归零。"""
        from code_tutor_agent.memory import _delta_start, empty_memory
        mem = empty_memory()
        mem["meta"]["watermark"] = {"session_id": "s1", "count": 10}
        assert _delta_start(mem, "s1", 3) == 0

    # ── transcript 过滤 ──

    def test_transcript_filters_system(self):
        from code_tutor_agent.memory import _format_transcript
        delta = [
            {"role": "system", "content": "题面 welcome 等不应被抽取的内容"},
            {"role": "user", "content": "我想做中等难度"},
            {"role": "tutor", "content": "好的"},
        ]
        text = _format_transcript(delta)
        assert "题面" not in text
        assert "用户: 我想做中等难度" in text
        assert "导师: 好的" in text

    # ── merge 防御 ──

    def test_merge_pref_whitelist(self):
        from code_tutor_agent.memory import MemoryExtraction, _merge, empty_memory
        ex = MemoryExtraction(
            unchanged=False,
            preferences={"hint_style": "socratic", "evil_key": "x", "language": ""},
        )
        new = _merge(empty_memory(), ex)
        assert new["preferences"] == {"hint_style": "socratic"}

    def test_merge_behavior_truncation(self):
        from code_tutor_agent.memory import (
            BEHAVIOR_CAP,
            OBSERVATIONS_CAP,
            MemoryExtraction,
            _merge,
            empty_memory,
        )
        ex = MemoryExtraction(
            unchanged=False,
            behavior=[f"b{i}" for i in range(12)],
            observations=[f"o{i}" for i in range(9)],
        )
        new = _merge(empty_memory(), ex)
        assert len(new["behavior"]) == BEHAVIOR_CAP
        assert len(new["observations"]) == OBSERVATIONS_CAP

    def test_merge_prefs_overlay_old(self):
        """LLM 未输出的旧偏好键应保留(新值按键覆盖)。"""
        from code_tutor_agent.memory import MemoryExtraction, _merge, empty_memory
        mem = empty_memory()
        mem["preferences"] = {"language": "python", "goals": "面试"}
        ex = MemoryExtraction(unchanged=False, preferences={"goals": "竞赛"})
        new = _merge(mem, ex)
        assert new["preferences"]["language"] == "python"
        assert new["preferences"]["goals"] == "竞赛"

    def test_has_signal_false_when_all_empty(self):
        """unchanged=False 但三通道全空 → 视为无信号(防误清)。"""
        from code_tutor_agent.memory import MemoryExtraction, _has_signal
        assert not _has_signal(MemoryExtraction(unchanged=False))
        assert not _has_signal(MemoryExtraction(unchanged=True, behavior=["x"]))
        assert _has_signal(MemoryExtraction(unchanged=False, behavior=["x"]))
        assert _has_signal(MemoryExtraction(unchanged=False, preferences={"language": "go"}))
        assert not _has_signal(MemoryExtraction(unchanged=False, preferences={"bad_key": "x"}))

    # ── DAO ──

    def test_dao_roundtrip(self):
        from code_tutor_agent.memory import load_memory, save_memory
        mem = load_memory()
        mem["preferences"] = {"hint_style": "socratic"}
        mem["behavior"] = ["看提示后自己先试"]
        mem["observations"] = ["常漏边界条件"]
        save_memory(mem)
        back = load_memory()
        assert back["preferences"] == {"hint_style": "socratic"}
        assert back["behavior"] == ["看提示后自己先试"]
        assert back["observations"] == ["常漏边界条件"]

    def test_dao_bad_json_fallback(self):
        from code_tutor_agent.db import database as dbmod
        from code_tutor_agent.db.database import MEMORY_USER_ID
        from code_tutor_agent.memory import load_memory
        conn = dbmod._get_conn()
        try:
            conn.execute(
                "INSERT INTO profiles (user_id, profile_json) VALUES (?, ?)",
                (MEMORY_USER_ID, "{bad json"),
            )
            conn.commit()
        finally:
            conn.close()
        mem = load_memory()  # 不应抛错
        assert mem["preferences"] == {}
        assert mem["behavior"] == []

    # ── 渲染 ──

    def test_render_empty(self):
        from code_tutor_agent.memory import render_memory_summary
        assert render_memory_summary() == ""

    def test_render_content(self):
        from code_tutor_agent.memory import load_memory, render_memory_summary, save_memory
        mem = load_memory()
        mem["preferences"] = {"hint_style": "引导式"}
        mem["behavior"] = ["看提示后自己先试"]
        mem["observations"] = ["对递归有进步"]
        save_memory(mem)
        text = render_memory_summary()
        assert "## 用户记忆" in text
        assert "提示风格: 引导式" in text
        assert "看提示后自己先试" in text
        assert "对递归有进步" in text

    # ── _run_extraction 流程(mock LLM)──

    def _payload(self, user_msgs: int, session_id: str = "s1"):
        messages = []
        for i in range(user_msgs):
            messages.append({"role": "user", "content": f"用户消息{i}"})
            messages.append({"role": "tutor", "content": f"导师消息{i}"})
        return {
            "session_id": session_id,
            "messages": messages,
            "verdict": "AC",
            "topic": "双指针",
            "difficulty": "medium",
            "hint_level": 1,
        }

    def test_gate_skip_no_llm_call(self, monkeypatch):
        """增量用户消息 <3 → 不调 LLM,但水位推进。"""
        import code_tutor_agent.memory as mem
        calls = []
        monkeypatch.setattr(mem, "_call_llm", lambda *a, **k: calls.append(a) or None)
        mem._run_extraction(self._payload(user_msgs=2))
        assert calls == []
        back = mem.load_memory()
        assert back["meta"]["watermark"] == {"session_id": "s1", "count": 4}

    def test_unchanged_advances_watermark_only(self, monkeypatch):
        import code_tutor_agent.memory as mem
        monkeypatch.setattr(
            mem, "_call_llm",
            lambda *a, **k: mem.MemoryExtraction(unchanged=True),
        )
        mem._run_extraction(self._payload(user_msgs=3))
        back = mem.load_memory()
        assert back["behavior"] == []
        assert back["meta"]["watermark"]["count"] == 6

    def test_signal_updates_memory(self, monkeypatch):
        import code_tutor_agent.memory as mem
        monkeypatch.setattr(
            mem, "_call_llm",
            lambda *a, **k: mem.MemoryExtraction(
                unchanged=False,
                preferences={"difficulty_preference": "medium"},
                behavior=["失败 3 次倾向换题"],
                observations=[],
            ),
        )
        mem._run_extraction(self._payload(user_msgs=3))
        back = mem.load_memory()
        assert back["preferences"] == {"difficulty_preference": "medium"}
        assert back["behavior"] == ["失败 3 次倾向换题"]
        assert back["meta"]["watermark"] == {"session_id": "s1", "count": 6}

    def test_llm_failure_degrades_to_watermark(self, monkeypatch):
        """LLM 层抛错(走真实 _call_llm 的兜底)→ 返回 None → 只推进水位,不崩。"""
        import sys
        import types

        import code_tutor_agent.memory as mem

        # 给 _call_llm 内部的 `from code_tutor_agent.config import get_llm`
        # 注入一个会抛错的 get_llm,模拟 LLM 不可用
        fake_config = types.ModuleType("code_tutor_agent.config")

        def boom(*a, **k):
            raise RuntimeError("llm down")

        fake_config.get_llm = boom
        monkeypatch.setitem(sys.modules, "code_tutor_agent.config", fake_config)

        mem._run_extraction(self._payload(user_msgs=3))  # 不应抛错
        back = mem.load_memory()
        assert back["meta"]["watermark"]["count"] == 6
        assert back["behavior"] == []

    def test_second_run_skips_already_extracted(self, monkeypatch):
        """同 session 水位推进后,同样长度的消息不再触发 LLM(delta 为空直接返回)。"""
        import code_tutor_agent.memory as mem
        calls = []

        def fake_llm(memory, transcript, payload):
            calls.append(transcript)
            return mem.MemoryExtraction(unchanged=True)

        monkeypatch.setattr(mem, "_call_llm", fake_llm)
        payload = self._payload(user_msgs=3)
        mem._run_extraction(payload)
        mem._run_extraction(payload)  # 第二次:水位已到位,delta 空
        assert len(calls) == 1

    # ── schedule_extraction 快照 ──

    def test_schedule_snapshots_state(self, monkeypatch):
        """state 在线程启动前被同步快照为纯 dict。"""
        import code_tutor_agent.memory as mem
        from code_tutor_agent.schemas.state import Message, ProblemMeta, SessionState

        captured = {}
        done = threading.Event()

        def fake_run(payload):
            captured.update(payload)
            done.set()

        monkeypatch.setattr(mem, "_run_extraction", fake_run)

        state = SessionState(
            session_id="sess-x",
            tutor_messages=[
                Message(role="user", content="你好"),
                Message(role="tutor", content="你好呀"),
            ],
            last_verdict="AC",
            hint_level=2,
            problem=ProblemMeta(
                problem_id=1, title="t", topic="双指针",
                difficulty="medium", description="d",
            ),
        )
        mem.schedule_extraction(state)
        assert done.wait(timeout=3), "extraction thread did not run"
        assert captured["session_id"] == "sess-x"
        assert captured["messages"] == [
            {"role": "user", "content": "你好"},
            {"role": "tutor", "content": "你好呀"},
        ]
        assert captured["verdict"] == "AC"
        assert captured["topic"] == "双指针"
        assert captured["hint_level"] == 2
