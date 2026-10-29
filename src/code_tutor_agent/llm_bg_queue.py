"""旁路（非请求路径）任务的有界后台队列。

历史上每次 /submit 都会裸起守护线程去调 LLM：错误模式分析（``purpose="edit-trace"``,
``max_tokens=16384``）与 AC 时的记忆抽取。它们不阻塞响应，但和用户请求**共用同一个
API key 的 RPM 桶**——连续提交时并发线程数没有上界，配额被旁路吃满后，下一个用户的
submit 直接撞 429，再叠上 SDK 重试与备用 key 全量重发，就是分钟级的白屏等待。

这里的口径：旁路永远排在用户请求之后。每个类别一条「单 worker + 有界队列」，
队列满则丢弃并计数，调用方永不阻塞。画像/记忆是尽力而为的衍生数据，丢一次不影响
做题；但让它们挤爆判题链路，影响的是所有人。

per-category 而非共享一个 worker：避免高频的错误模式分析把低频的记忆抽取饿死。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# 每类别队列长度。判题旁路的成本是一个大 prompt 的 LLM 往返（数十秒），
# 16 条已经足够跨过任何瞬时抖动；再长只会让数据更陈旧。
_DEFAULT_QUEUE_SIZE = max(1, int(os.getenv("LLM_BG_QUEUE_SIZE", "16")))

# name → (queue, worker 线程)
_QUEUES: dict[str, queue.Queue] = {}
_WORKERS: dict[str, threading.Thread] = {}
_START_LOCK = threading.Lock()

# 丢弃计数（按类别），仅用于日志/排障，不对外承诺指标口径。
_dropped: dict[str, int] = {}


def _record_dropped(name: str) -> None:
    _dropped[name] = _dropped.get(name, 0) + 1
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        get_registry().record("llm_bg_dropped")
    except Exception:
        pass


def _drain(name: str, q: queue.Queue) -> None:
    """worker 主循环：任务内异常一律吞掉，绝不让 worker 线程因单个任务而死。"""
    while True:
        fn = q.get()
        try:
            if fn is not None:
                fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bg queue %r task failed (non-fatal): %s", name, exc)
        finally:
            q.task_done()


def submit_bounded(name: str, fn: Callable[[], object]) -> bool:
    """把 ``fn`` 排进类别 ``name`` 的后台队列。队列满则丢弃并返回 False。

    本函数只做入队，永不阻塞调用方（判题 worker 线程不能被旁路拖住）。
    """
    with _START_LOCK:
        q = _QUEUES.get(name)
        if q is None:
            q = queue.Queue(maxsize=_DEFAULT_QUEUE_SIZE)
            _QUEUES[name] = q
            t = threading.Thread(target=_drain, args=(name, q), daemon=True, name=f"bg-{name}")
            _WORKERS[name] = t
            t.start()
    try:
        q.put_nowait(fn)
        return True
    except queue.Full:
        _record_dropped(name)
        logger.warning(
            "bg queue %r full (dropped total=%d) — 旁路任务让位给用户请求",
            name, _dropped.get(name, 0),
        )
        return False


def pending_counts() -> dict[str, int]:
    """各类别排队深度（监控/测试用）。"""
    return {name: q.qsize() for name, q in _QUEUES.items()}
