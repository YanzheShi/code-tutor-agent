"""异步批量落库:把 LLM 用量明细旁路写入 token_usage 表。

设计目标(见 docs/token-cost-control-design.md §5):
- 零侵入:``enqueue(record)`` 仅 ``queue.put``,主流程零等待。
- 不阻塞返回:后台线程每 ``FLUSH_INTERVAL`` 秒或队列满 ``FLUSH_BATCH`` 条时
  批量 ``executemany`` 写明细,并 ``UPSERT`` 汇总到 ``token_usage_daily``。
- 防丢尾:进程退出前 ``atexit`` flush 余量。
- 复用 edit-trace 的 flush 范式(WAL + 后台线程)。

线程模型:单例 ``token_sink``;模块导入即启动后台线程(守护线程,不阻止退出)。
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = float(os.getenv("TOKEN_FLUSH_INTERVAL", "2.0"))   # 秒
FLUSH_BATCH = int(os.getenv("TOKEN_FLUSH_BATCH", "200"))           # 条

# 明细落库字段(顺序须与 database.insert_token_usage_batch 的占位一致)
_RECORD_FIELDS = (
    "session_id", "user_id", "purpose", "model_alias", "model_name",
    "mode", "topic", "difficulty", "problem_id",
    "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens",
    "total_tokens", "cost", "latency_ms", "run_id",
)


class TokenSink:
    """单例:内存队列 + 后台 flush 线程。

    线程模型:后台线程独占 drain(持有 ``_drain_lock``),
    ``flush_nowait`` 先唤醒后台线程、等它 drain 干净再兜底。
    避免 atexit 主线程与后台线程并发写库造成的重复/遗漏。
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[dict]" = queue.Queue()
        # 保护 _stop 标志翻转,防止后台线程双判
        self._stop_lock = threading.Lock()
        # 互斥 drain:同一时刻最多一个 drain 在执行
        self._drain_lock = threading.Lock()
        # 可中断等待:后台线程 sleep,atexit 时 set() 即时唤醒,避免进程退出拖尾
        self._flush_ev = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="token-sink", daemon=True)
        self._thread.start()
        atexit.register(self.flush_nowait)

    def enqueue(self, record: dict) -> None:
        """入队一条用量记录(非阻塞)。"""
        try:
            self._queue.put_nowait(record)
        except Exception as exc:  # pragma: no cover - 队列异常不应影响主流程
            logger.warning("[token_sink] enqueue failed (ignored): %s", exc)

    def pending(self) -> int:
        return self._queue.qsize()

    # ── 内部 ──
    def _run(self) -> None:
        while True:
            # Event.wait 可被 set() 即时唤醒(用于 atexit),避免进程退出
            # 时后台线程卡住 FLUSH_INTERVAL 秒拖慢 teardown。
            try:
                self._flush_ev.wait(FLUSH_INTERVAL)
            except Exception:
                pass
            if self._stopped():
                # 退出前再做一次 drain,确保尾数据落库,然后退出线程
                self._drain()
                break
            self._drain()
            self._flush_ev.clear()  # 重置,等下一个周期

    def _stopped(self) -> bool:
        with self._stop_lock:
            return self._stop

    def _drain(self) -> None:
        """取出当前队列中所有记录并批量落库。

        加 ``_drain_lock`` 互斥,保证同一时刻只有一条 drain 在执行,
        主线程与后台线程不会并发写库。
        """
        if not self._drain_lock.acquire(blocking=False):
            return  # 已有 drain 在执行,本次让出
        try:
            batch: list[dict] = []
            try:
                while len(batch) < FLUSH_BATCH:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                return
            # 过滤缺 purpose 的脏数据(防御性)
            batch = [b for b in batch if b and b.get("purpose")]
            if not batch:
                return
            self._write(batch)
        finally:
            self._drain_lock.release()

    def _write(self, batch: list[dict]) -> None:
        try:
            # 延迟导入,避免与 db 模块的循环依赖,并确保表已建
            from code_tutor_agent.db import database

            database.init_db()
            rows = [tuple(rec.get(f, 0 if f not in (
                "session_id", "user_id", "purpose", "model_alias", "model_name",
                "mode", "topic", "difficulty", "run_id") else "") for f in _RECORD_FIELDS)
                    for rec in batch]
            database.insert_token_usage_batch(rows)
            daily = self._aggregate_daily(batch)
            if daily:
                database.upsert_token_usage_daily_batch(daily)
        except Exception as exc:  # 落库失败绝不抛回主流程
            logger.warning("[token_sink] flush failed (dropped %d records): %s",
                           len(batch), exc)

    @staticmethod
    def _aggregate_daily(batch: list[dict]) -> list[tuple]:
        """按 (day, purpose, model_alias, user_id) 预聚合,供 daily 表 UPSERT。"""
        from datetime import datetime, timezone

        buckets: dict[tuple, dict] = {}
        for rec in batch:
            day = rec.get("ts_day") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            key = (
                day,
                rec.get("purpose", "unknown"),
                rec.get("model_alias", ""),
                rec.get("user_id", "default"),
            )
            b = buckets.setdefault(key, {
                "call_count": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0,
            })
            b["call_count"] += 1
            b["prompt_tokens"] += int(rec.get("prompt_tokens", 0) or 0)
            b["completion_tokens"] += int(rec.get("completion_tokens", 0) or 0)
            b["cache_creation_tokens"] += int(rec.get("cache_creation_tokens", 0) or 0)
            b["cache_read_tokens"] += int(rec.get("cache_read_tokens", 0) or 0)
            b["cost"] += float(rec.get("cost", 0.0) or 0.0)
        return [
            (k[0], k[1], k[2], k[3],
             v["call_count"], v["prompt_tokens"], v["completion_tokens"],
             v["cache_creation_tokens"], v["cache_read_tokens"], round(v["cost"], 6))
            for k, v in buckets.items()
        ]

    def flush_nowait(self) -> None:
        """立即把剩余记录落库(供 atexit 调用)。

        置 _stop + set 事件唤醒后台线程 → 后台线程 drain 干净并退出;
        主线程 join 等它收尾,兜底再做一次 drain(持锁,若后台已处理则让出),
        不重复写库,且不阻塞进程退出。
        """
        with self._stop_lock:
            self._stop = True
        self._flush_ev.set()  # 唤醒可能正 wait 的后台线程
        try:
            self._thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            self._drain()
        except Exception as exc:  # pragma: no cover
            logger.warning("[token_sink] final flush failed: %s", exc)


# ── 单例 ──
_token_sink_instance: "TokenSink | None" = None
_token_sink_lock = threading.Lock()


def get_token_sink() -> TokenSink:
    """获取(惰性创建)TokenSink 单例。"""
    global _token_sink_instance
    if _token_sink_instance is None:
        with _token_sink_lock:
            if _token_sink_instance is None:
                _token_sink_instance = TokenSink()
    return _token_sink_instance


# 模块导入即可用的便捷引用
token_sink = get_token_sink()
