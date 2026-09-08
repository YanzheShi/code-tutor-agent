"""业务配额（F-04 安全加固，2026-09-08）。

背景：chat/submit 等业务端点此前无用户级频控（审计发现 F-04），单账号可
脚本化烧穿 LLM 成本。本模块提供三类业务配额（纯内存，单进程部署口径，
与 auth.rate_limit 同一实现惯例）：

- 做题配额：滚动 1h / 滚动 24h 双窗口各 5 道新题（用户维度 + IP 维度分别计量）
- 每题提问：每道题最多 20 次导师提问（仅 chat/stream 计数；submit/run 不计数）
- 轨迹追问：每题最多 20 次追问（仅 analyze 系列带 message 的调用计数，首轮不计）

扣减点设计（每道新题恰好计 1，防双计/防绕过）：
- create_session：后台 run_generation 立即绑题 → 计 1
- by-problem：立即绑题 → 计 1
- next-problem：仅重置回对话、不绑题 → 不计（绑题发生在 chat 出题分支，彼时计 1）
- chat/stream 意图判定 is_ready：新题绑定点 → 计 1

豁免规则（用户确认 2026-09-08）：
- 自带 API key 的用户（custom LLM 模式，runtime_settings.llm_override_ctx 生效中）
  不受限——配额目的是保护服务器 LLM 成本，自带 key 不产生服务器成本
- 超限只挡「新题 / 对话 / 追问」，提交与运行不受影响（文案按用户要求给出路）

环境变量（值 ≤0 = 关闭该项；测试由 tests/conftest.py 关总闸）：
- CTA_QUOTA_ENABLED          总闸，默认 1
- CTA_QUOTA_PROBLEM_USER     用户维度做题数/窗口，默认 5
- CTA_QUOTA_PROBLEM_IP       IP 维度做题数/窗口，默认 5（NAT 多人同 IP 场景可调大或置 0 关闭 IP 维度）
- CTA_QUOTA_PROBLEM_WINDOW_H / _D   两窗口秒数，默认 3600 / 86400
- CTA_QUOTA_CHAT_USER / _IP      每题提问上限，默认 20
- CTA_QUOTA_TRACE_USER / _IP     每题追问上限，默认 20
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from code_tutor_agent.api.auth import _client_ip

_lock = threading.Lock()

# 滑动窗口桶：key -> 时间戳队列（与 auth._RATE_BUCKETS 同构）
_windows: dict[str, deque] = {}
# 生命周期计数器：key -> [count, last_seen_ts]（每题提问/追问，无时间窗）
_counters: dict[str, list] = {}

_COUNTER_IDLE_TTL = 7 * 86400  # 计数器闲置 7 天后可被清理
_PRUNE_THRESHOLD = 4096        # 桶数量超过该值时触发一次清理


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _enabled() -> bool:
    return _env_int("CTA_QUOTA_ENABLED", 1) > 0


def _custom_llm_active() -> bool:
    """当前请求是否使用用户自带 LLM 配置（中间件已按 Bearer 注入 ContextVar）。"""
    from code_tutor_agent.runtime_settings import get_llm_override

    return get_llm_override() is not None


# ── 友好提示文案（超限只挡对话，按用户要求给出路）──

MSG_PROBLEM_LIMIT = (
    "今天的做题额度已用完（每 24 小时最多 {limit} 道新题）。"
    "先消化一下已做过的题、把提示要点再过一遍，明天继续加油！"
)
MSG_CHAT_LIMIT = (
    "本题的导师提问次数已达上限（{limit} 次）。导师的提示已经很充足了，"
    "先试着改改代码、运行验证一下吧！也可以直接提交看判题反馈，或换一题继续。"
)
MSG_TRACE_LIMIT = (
    "本题的轨迹追问次数已达上限（{limit} 次）。"
    "建议结合分析要点先自己动手验证，或换一题继续练习～"
)


class _Window:
    """单个滑动窗口的检查+扣减（调用方需持有 _lock）。"""

    __slots__ = ("env", "default", "window_env", "window_default", "kind_env")

    def __init__(self, env: str, default: int, window_env: str, window_default: int):
        self.env = env
        self.default = default
        self.window_env = window_env
        self.window_default = window_default


def _limits(env_user: str, default_user: int, env_ip: str, default_ip: int,
            window_env: str = "", window_default: int = 0):
    """读取用户/IP 两维限额；(limit, window)。limit≤0 表示该维度关闭。"""
    limit = _env_int(env_user, default_user)
    if env_ip:
        ip_limit = _env_int(env_ip, default_ip)
    else:
        ip_limit = limit
    window = _env_int(window_env, window_default) if window_env else 0
    return limit, ip_limit, window


def _prune_locked(now: float) -> None:
    """桶数量过大时清理过期项（惰性触发，避免后台线程）。"""
    if len(_windows) > _PRUNE_THRESHOLD:
        stale = [k for k, q in _windows.items() if not q or q[-1] < now - 86400 * 2]
        for k in stale:
            _windows.pop(k, None)
    if len(_counters) > _PRUNE_THRESHOLD:
        stale = [k for k, v in _counters.items() if now - v[1] > _COUNTER_IDLE_TTL]
        for k in stale:
            _counters.pop(k, None)


def _take_window(key: str, limit: int, window: float, now: float) -> None:
    """检查并扣减一个滑动窗口槽位；超限抛 429（调用方需持有 _lock）。"""
    bucket = _windows.setdefault(key, deque())
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, MSG_PROBLEM_LIMIT.format(limit=limit))
    bucket.append(now)


def _take_counter(key: str, limit: int, msg: str) -> None:
    """检查并扣减一个生命周期计数；超限抛 429。"""
    entry = _counters.setdefault(key, [0, time.monotonic()])
    if entry[0] >= limit:
        raise HTTPException(429, msg.format(limit=limit))
    entry[0] += 1
    entry[1] = time.monotonic()


def reset_all() -> None:
    """清空全部配额状态（仅供测试隔离使用）。"""
    with _lock:
        _windows.clear()
        _counters.clear()


# ── 对外检查入口（超限直接抛 HTTPException 429 + 友好文案）──


def check_problem_start(request: Request | None, uid: str) -> None:
    """开始做一道新题的配额检查（滚动 1h + 滚动 24h 双窗口）。

    扣减点：create_session / by-problem / chat 出题分支（见模块 docstring）。
    """
    if not _enabled() or _custom_llm_active():
        return
    limit, ip_limit, _ = _limits("CTA_QUOTA_PROBLEM_USER", 5, "CTA_QUOTA_PROBLEM_IP", 5)
    if limit <= 0 and ip_limit <= 0:
        return
    win_h = _env_int("CTA_QUOTA_PROBLEM_WINDOW_H", 3600)
    win_d = _env_int("CTA_QUOTA_PROBLEM_WINDOW_D", 86400)
    ip = _client_ip(request)
    now = time.monotonic()
    with _lock:
        _prune_locked(now)
        # 全部窗口先检查后扣减：任一超限即整体拒绝（不产生半扣状态）
        if limit > 0:
            _take_window(f"pu:{uid}:h", limit, win_h, now)
            _take_window(f"pu:{uid}:d", limit, win_d, now)
        if ip_limit > 0 and ip != "direct":
            _take_window(f"pi:{ip}:h", ip_limit, win_h, now)
            _take_window(f"pi:{ip}:d", ip_limit, win_d, now)


def check_chat_ask(request: Request | None, uid: str, problem_id) -> None:
    """每题导师提问配额（仅 chat/stream 调用；problem_id 为空=未绑题，不计量）。"""
    if not problem_id or not _enabled() or _custom_llm_active():
        return
    limit, ip_limit, _ = _limits("CTA_QUOTA_CHAT_USER", 20, "CTA_QUOTA_CHAT_IP", 20)
    if limit <= 0 and ip_limit <= 0:
        return
    pid = str(problem_id)
    ip = _client_ip(request)
    msg = MSG_CHAT_LIMIT
    with _lock:
        _prune_locked(time.monotonic())
        if limit > 0:
            _take_counter(f"cu:{uid}:{pid}", limit, msg)
        if ip_limit > 0 and ip != "direct":
            _take_counter(f"ci:{ip}:{pid}", ip_limit, msg)


def check_trace_followup(request: Request | None, uid: str, problem_id) -> None:
    """每题轨迹追问配额（仅 analyze 系列带 message 的追问；首轮结构化分析不计）。"""
    if not problem_id or not _enabled() or _custom_llm_active():
        return
    limit, ip_limit, _ = _limits("CTA_QUOTA_TRACE_USER", 20, "CTA_QUOTA_TRACE_IP", 20)
    if limit <= 0 and ip_limit <= 0:
        return
    pid = str(problem_id)
    ip = _client_ip(request)
    msg = MSG_TRACE_LIMIT
    with _lock:
        _prune_locked(time.monotonic())
        if limit > 0:
            _take_counter(f"tu:{uid}:{pid}", limit, msg)
        if ip_limit > 0 and ip != "direct":
            _take_counter(f"ti:{ip}:{pid}", ip_limit, msg)
