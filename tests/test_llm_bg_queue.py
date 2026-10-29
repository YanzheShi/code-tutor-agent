"""Tests for the bounded side-channel LLM queue（llm_bg_queue，2026-10-27）。

旁路任务（错误模式分析 / 记忆抽取）与用户判题共用同一个 key 的 RPM 桶。这里要保证的是
两条硬性质：**调用方永不阻塞**、**队列满就丢**（宁可少一次画像，也不能让下一个人白屏）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from code_tutor_agent import llm_bg_queue as q


def test_runs_task_in_background():
    done: list[int] = []

    assert q.submit_bounded("t-run", lambda: done.append(1)) is True
    for _ in range(50):
        if done:
            break
        time.sleep(0.05)
    assert done == [1]


def test_task_exception_does_not_kill_worker():
    """worker 因单个任务异常而死 = 之后所有旁路静默堆积到满再全丢，最难查的那种故障。"""
    def _boom():
        raise RuntimeError("模拟旁路 LLM 抛错")

    q.submit_bounded("t-survive", _boom)
    done: list[int] = []
    assert q.submit_bounded("t-survive", lambda: done.append(2)) is True
    for _ in range(50):
        if done:
            break
        time.sleep(0.05)
    assert done == [2], "前一个任务抛错后 worker 必须还活着"


def test_drops_when_queue_full_and_never_blocks(monkeypatch):
    """满则丢：入队是非阻塞调用，且返回值如实反映被丢。

    注意 _DEFAULT_QUEUE_SIZE 在**建队列时**读取，所以必须在第一次 submit 之前打桩。
    """
    name = "t-full"
    monkeypatch.setattr(q, "_DEFAULT_QUEUE_SIZE", 2)

    def _slow():
        time.sleep(0.6)  # 占住单 worker，让队列真的排起来

    results = [q.submit_bounded(name, _slow) for _ in range(30)]

    assert False in results, "排满后必须开始丢弃而不是无限排队"
    assert all(isinstance(r, bool) for r in results)
    assert q._dropped[name] > 0
    assert q.pending_counts().get(name, 0) <= 2


class TestCallSitesAreWired:
    """两个旁路调用点必须**真的**走队列。

    只测队列本身不够：谁把 memory / 错误模式分析改回裸 threading.Thread，
    队列的测试照样全绿，而线上又会开始抢判题的 RPM 桶。
    """

    def test_memory_extraction_goes_through_queue(self, monkeypatch):
        from code_tutor_agent import memory

        seen: list[tuple[str, object]] = []
        monkeypatch.setattr(q, "submit_bounded",
                            lambda name, fn: seen.append((name, fn)) or True)
        memory.schedule_extraction(SimpleNamespace(
            session_id="s1", user_id="u1", last_verdict="AC", hint_level=0,
            tutor_messages=[], problem=None,
        ))
        assert seen and seen[0][0] == "memory-extract"
        assert callable(seen[0][1])

    def test_error_mode_analysis_goes_through_queue(self, monkeypatch):
        from code_tutor_agent.profile import edit_trace_analyzer as eta

        seen: list[str] = []
        monkeypatch.setattr(q, "submit_bounded",
                            lambda name, fn: seen.append(name) or True)
        eta.fire_and_forget_error_mode_analysis(
            "s1", topic="数组", description="d", final_code="c", verdict="WA",
        )
        assert seen == ["error-mode"]

    def test_no_bare_thread_is_spawned(self, monkeypatch):
        """裸 Thread 一旦回来，并发数就没有上界了 —— 从源头掐断。"""
        import threading

        from code_tutor_agent import memory

        monkeypatch.setattr(q, "submit_bounded", lambda name, fn: True)
        started: list[str] = []
        real = threading.Thread

        class _Spy(real):
            def start(self):
                started.append(self.name)
                super().start()

        monkeypatch.setattr(threading, "Thread", _Spy)
        memory.schedule_extraction(SimpleNamespace(
            session_id="s1", user_id="u1", last_verdict="AC", hint_level=0,
            tutor_messages=[], problem=None,
        ))
        assert started == [], f"schedule_extraction 又去裸起线程了: {started}"
