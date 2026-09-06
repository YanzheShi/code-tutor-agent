"""进程内轻量指标注册表（监控告警 P0，见 docs/monitoring-alerts-design.md §4）。

设计口径：
- 线程安全（threading.Lock）：middleware（事件循环）、token sink 线程、判题线程都会写。
- **永不抛异常**：record / record_streak / set_gauge 任何内部错误只记 debug 日志，
  对齐 observability.py 的「可观测层故障绝不影响主流程」原则。
- 窗口只保留 10 分钟内存明细，进程重启清零（接受：重启本身由进程外心跳兜底）。
- streak（连续失败计数）与窗口计数并存：streak 用于「某环节连续挂」场景，
  成功一次即清零，不依赖流量基数。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

# 滑动窗口保留时长（秒）。规则最长看 10 分钟，多留 1 倍余量防边界抖动。
_WINDOW_SECONDS = 1200.0
# 单窗口明细上限（防内存膨胀；正常流量远达不到）
_WINDOW_MAX_ITEMS = 20000


def _now() -> float:
    return time.monotonic()


class Window:
    """滑动窗口：保留最近 _WINDOW_SECONDS 的 (timestamp, value) 明细。

    独立加锁（Registry 的锁不跨 Window.add，避免长持锁）。
    """

    def __init__(self, window_seconds: float = _WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._items: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def add(self, value: float = 1.0, ts: float | None = None) -> None:
        ts = _now() if ts is None else ts
        with self._lock:
            items = self._items
            items.append((ts, value))
            while len(items) > _WINDOW_MAX_ITEMS:
                items.popleft()
            self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window_seconds
        items = self._items
        while items and items[0][0] < cutoff:
            items.popleft()

    def count(self, seconds: float = 300.0, ts: float | None = None) -> int:
        """最近 seconds 秒内的取值总和（value=1 时即次数）。"""
        ts = _now() if ts is None else ts
        cutoff = ts - seconds
        with self._lock:
            self._evict(ts)
            return int(sum(v for t, v in self._items if t >= cutoff))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class MetricsRegistry:
    """计数器 + 滑动窗口 + streak + gauge 的进程内注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._windows: dict[str, Window] = {}
        self._streaks: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._started_at = time.time()

    # ── 写入 ────────────────────────────────────────────────
    def record(self, name: str, value: float = 1.0) -> None:
        """累计计数 + 窗口明细。任何异常只记 debug，绝不外抛。"""
        try:
            with self._lock:
                self._counters[name] = self._counters.get(name, 0) + value
                win = self._windows.get(name)
                if win is None:
                    win = Window()
                    self._windows[name] = win
            win.add(value)
        except Exception:  # pragma: no cover - 防御性
            logger.debug("[metrics] record(%s) failed (ignored)", name, exc_info=True)

    def record_streak(self, name: str, ok: bool) -> int:
        """连续失败计数：失败 streak+1 返回当前值；成功清零返回 0。"""
        try:
            with self._lock:
                if ok:
                    self._streaks[name] = 0
                    return 0
                cur = self._streaks.get(name, 0) + 1
                self._streaks[name] = cur
                return cur
        except Exception:  # pragma: no cover - 防御性
            logger.debug("[metrics] record_streak(%s) failed (ignored)", name, exc_info=True)
            return 0

    def set_gauge(self, name: str, value: float) -> None:
        """设置瞬时值（自检结果、磁盘使用率等）。"""
        try:
            with self._lock:
                self._gauges[name] = value
        except Exception:  # pragma: no cover - 防御性
            logger.debug("[metrics] set_gauge(%s) failed (ignored)", name, exc_info=True)

    # ── 读取 ────────────────────────────────────────────────
    def window_count(self, name: str, seconds: float = 300.0) -> int:
        try:
            with self._lock:
                win = self._windows.get(name)
            if win is None:
                return 0
            return win.count(seconds)
        except Exception:  # pragma: no cover - 防御性
            return 0

    def streak(self, name: str) -> int:
        try:
            with self._lock:
                return self._streaks.get(name, 0)
        except Exception:  # pragma: no cover - 防御性
            return 0

    def gauge(self, name: str) -> float | None:
        try:
            with self._lock:
                return self._gauges.get(name)
        except Exception:  # pragma: no cover - 防御性
            return None

    def snapshot(self) -> dict:
        """给 /admin/metrics 与规则评估用的快照。

        返回结构：
        {
          "uptime_seconds": ...,
          "counters": {name: total},
          "streaks": {name: n},
          "gauges": {name: v},
          "windows": {name: {"5m": n, "10m": n}},
        }
        """
        data: dict = {}
        try:
            with self._lock:
                counters = dict(self._counters)
                streaks = dict(self._streaks)
                gauges = dict(self._gauges)
                windows = dict(self._windows)
            now = _now()
            data = {
                "uptime_seconds": round(time.time() - self._started_at),
                "counters": counters,
                "streaks": streaks,
                "gauges": gauges,
                "windows": {
                    name: {
                        "5m": win.count(300.0, now),
                        "10m": win.count(600.0, now),
                    }
                    for name, win in windows.items()
                },
            }
        except Exception:  # pragma: no cover - 防御性
            logger.debug("[metrics] snapshot failed (ignored)", exc_info=True)
        return data


# ── 进程级单例 ──────────────────────────────────────────────
_REGISTRY: MetricsRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> MetricsRegistry:
    """返回进程级 MetricsRegistry 单例。"""
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = MetricsRegistry()
    return _REGISTRY


def record_graph_call(entry: str, ok: bool) -> None:
    """LangGraph 主流程调用埋点：graph_ok/graph_fail 计数 + graph_fail streak。

    entry 为调用点标识（"generation"/"run_resume"/"submit"/"session_create"/…），
    仅进日志详情，不进指标 key（避免维度爆炸）。永不抛异常。
    """
    try:
        reg = get_registry()
        reg.record("graph_ok" if ok else "graph_fail")
        reg.record_streak("graph_fail", ok)
        if not ok:
            logger.warning("[metrics] graph invoke failed (entry=%s)", entry or "unknown")
    except Exception:  # pragma: no cover - 防御性
        pass
