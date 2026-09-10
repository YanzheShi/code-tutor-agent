"""业务配额（F-04 安全加固，2026-09-08）。

背景：chat/submit 等业务端点此前无用户级频控（审计发现 F-04），单账号可
脚本化烧穿 LLM 成本。本模块提供三类业务配额（纯内存，单进程部署口径，
与 auth.rate_limit 同一实现惯例）：

- 做题配额：滚动 1h / 滚动 24h 双窗口各 5 道新题（用户维度 + IP 维度分别计量）
- 每题提问：每道题最多 20 次导师提问（仅 chat/stream 计数；submit/run 不计数）
- 轨迹追问：每题最多 20 次追问（仅 analyze 系列带 message 的调用计数，首轮不计）
- 判题限频：submit/run 每用户滚动窗口 20 次/小时（默认；不豁免自带 key 用户——
  判题 CPU 是服务器自身资源，见 check_judge）

IP 维度口径（2026-09-10 起）：**仅对测试用户（体验账号，role='test'）生效**——
清 localStorage 即换新身份，需要不可自选的锚防刷；注册用户纯用户维度
（校园网/NAT 多人同公网 IP 会误杀，见 2026-09-09 调研）。测试用户触额文案
走转化钩子（引导注册），注册=原地转正后配额桶清零重新开闸。

扣减点设计（每道新题恰好计 1，防双计/防绕过）：
- create_session：后台 run_generation 立即绑题（生成新题）→ 计 1
- by-problem：从题库选题复用已有题面，**不消耗新题生成的 LLM 成本**（2026-09-10 起
  豁免做题配额）→ 不计（触额后仍可选题；运行/提交/提问仍受各自配额约束）
- next-problem：仅重置回对话、不绑题 → 不计（绑题发生在 chat 出题分支，彼时计 1）
- chat/stream 意图判定 is_ready：新题绑定点（生成新题）→ 计 1

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
- CTA_QUOTA_SUBMIT_USER          判题限频（次/窗口），默认 20
- CTA_QUOTA_SUBMIT_WINDOW        判题限频窗口秒数，默认 3600（1 小时）
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


class QuotaExceeded(HTTPException):
    """配额超限（429）统一异常：携带友好文案 + 结构化恢复信息。

    额外属性（供调用方/测试读取，不影响 FastAPI 序列化——前端只取 status_code/detail）：
    - used / limit：当前窗口已用 / 上限
    - reset_in：最早一条记录还有多少秒过期（None=无时间窗，如每题生命周期计数）
    - window_label：触发窗口的人类可读标签（"今天"/"本小时"）
    - can_use_custom_llm：开启自定义 LLM 是否可豁免本限制（仅做题/提问/追问类配额适用）
    """

    def __init__(
        self,
        detail: str,
        *,
        reset_in: int | None = None,
        used: int | None = None,
        limit: int | None = None,
        window_label: str | None = None,
        can_use_custom_llm: bool = False,
    ):
        headers = {"Retry-After": str(int(reset_in))} if reset_in else None
        super().__init__(status_code=429, detail=detail, headers=headers)
        self.used = used
        self.limit = limit
        self.reset_in = reset_in
        self.window_label = window_label
        self.can_use_custom_llm = can_use_custom_llm


# ── 友好提示文案（超限只挡对话，按用户要求给出路）──
# 全部带「已用/上限」与恢复出路；{used}/{limit}/{window_label}/{reset_min} 由调用方格式化。

MSG_PROBLEM_LIMIT = (
    "做题额度已用完（{window_label}已做 {used}/{limit} 道新题）。"
    "开启「自定义 LLM」（在设置里填自己的 API Key）即可不受做题额度限制、继续练习；"
    "或等明天额度重置后再来（每天最多 {limit} 道，本小时额度约 {reset_min} 分钟后先恢复）。"
)
MSG_CHAT_LIMIT = (
    "本题的导师提问次数已达上限（已问 {used}/{limit} 次）。导师的提示已经很充足了，"
    "先试着改改代码、运行验证一下吧！也可以直接提交看判题反馈，或换一题继续。"
)
MSG_TRACE_LIMIT = (
    "本题的轨迹追问次数已达上限（已追问 {used}/{limit} 次）。"
    "建议结合分析要点先自己动手验证，或换一题继续练习～"
)
MSG_JUDGE_LIMIT = (
    "提交/运行太频繁了（本小时已用 {used}/{limit} 次，已达上限）。"
    "请稍后再试，约 {reset_min} 分钟后额度自动恢复～"
)

# 测试用户（免注册试用）专属文案：触额即转化钩子——引导注册而非「明天再来」。
# 注册=原地转正（user_id 不变），做题记录全保留。
MSG_PROBLEM_LIMIT_TRIAL = (
    "体验额度已用完（同一网络下的体验额度共享）。注册正式账号即可继续练习，"
    "你在这里做过的题和记录会完整保留！"
)
MSG_CHAT_LIMIT_TRIAL = (
    "体验账号的提问次数已达上限（已问 {used}/{limit} 次）。注册正式账号后继续本题，"
    "对话与做题记录都会完整保留！"
)
MSG_TRACE_LIMIT_TRIAL = (
    "体验账号的轨迹追问次数已达上限（已追问 {used}/{limit} 次）。注册正式账号后可继续追问，"
    "记录会完整保留！"
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


def _window_status(key: str, limit: int, window: float, now: float) -> tuple[int, int, float]:
    """只读计算单个滑动窗口的 (used, limit, reset_in_seconds)，不修改桶（调用方持锁）。

    used = 窗口内仍有效的记录数；reset_in = 最早一条记录还有多久过期（秒，向上取整前的原始值）。
    窗口未触发（used<limit）时 reset_in 仍反映最早记录过期时间，用于文案「约 N 分钟恢复」。
    """
    bucket = _windows.get(key)
    if not bucket:
        return 0, limit, 0.0
    cutoff = now - window
    live = [t for t in bucket if t >= cutoff]
    used = len(live)
    if used == 0:
        return 0, limit, 0.0
    reset_in = max(0.0, (live[0] + window) - now)
    return used, limit, reset_in


def _take_counter(key: str, limit: int, msg: str, can_use_custom_llm: bool = False) -> None:
    """检查并扣减一个生命周期计数；超限抛 QuotaExceeded(429)（调用方需持有 _lock）。"""
    entry = _counters.setdefault(key, [0, time.monotonic()])
    used = entry[0]
    if used >= limit:
        detail = msg.format(used=used, limit=limit)
        raise QuotaExceeded(detail, used=used, limit=limit, can_use_custom_llm=can_use_custom_llm)
    entry[0] += 1
    entry[1] = time.monotonic()


def check_judge(request: Request | None, uid: str) -> None:
    """提交/运行判题限频：按用户滑动窗口（默认 20 次/小时）。

    ⚠️ 与其他配额的豁免规则**刻意不同**：判题 CPU 是服务器自身资源（与
    LLM 成本不同源），本配额**不豁免**自带 API key 的用户（2026-09-08 确认）。
    扣减点：/session/{sid}/submit 与 /session/{sid}/run 入口。
    超限返回 429 + 友好文案（含已用/上限/恢复时间，附标准 Retry-After 头）。
    """
    if not _enabled():
        return
    limit = _env_int("CTA_QUOTA_SUBMIT_USER", 20)
    if limit <= 0:
        return
    window = _env_int("CTA_QUOTA_SUBMIT_WINDOW", 3600)
    now = time.monotonic()
    key = f"ju:{uid}"
    with _lock:
        _prune_locked(now)
        used, lim, reset = _window_status(key, limit, window, now)
        if used >= limit:
            detail = MSG_JUDGE_LIMIT.format(
                used=used, limit=limit, reset_min=max(1, int(reset // 60)))
            raise QuotaExceeded(
                detail, reset_in=int(reset), used=used, limit=limit, window_label="本小时")
        # 未触顶 → 扣减（持锁内原子，先查后扣不产生半扣状态）
        _windows.setdefault(key, deque()).append(now)


def reset_all() -> None:
    """清空全部配额状态（仅供测试隔离使用）。"""
    with _lock:
        _windows.clear()
        _counters.clear()


def reset_user(uid: str) -> None:
    """清空某用户的全部配额桶（测试用户转正后重新开闸；仅供 /auth/claim 调用）。

    按 key 分段精确匹配 uid（用户桶 pu/cu/tu/ju 都含 uid 段）；
    IP 桶 pi/ci 的分段是 IP（含点号，与纯数字 uid 不同段）不会被误清。
    """
    with _lock:
        for k in [k for k in _windows if uid in k.split(":")]:
            _windows.pop(k, None)
        for k in [k for k in _counters if uid in k.split(":")]:
            _counters.pop(k, None)


# ── 对外检查入口（超限直接抛 HTTPException 429 + 友好文案）──


def check_problem_start(request: Request | None, uid: str, is_test: bool = False) -> None:
    """开始做一道新题（**仅新题生成**）的配额检查（滚动 1h + 滚动 24h 双窗口）。

    扣减点：create_session（topic 出题）/ chat 出题分支。
    ⚠️ by-problem（从题库选题）**不调用本函数**——选题复用已有题面、不消耗
    新题生成的 LLM 成本，故豁免做题配额（触额后仍可选题；运行/提交/提问仍受各自配额约束）。
    is_test（体验账号）：IP 维度**仅对测试用户生效**（清 localStorage 即换新身份，
    需不可自选的锚防刷；注册用户纯用户维度，校园网/NAT 误杀只与注册用户相关）。
    触额时文案走转化钩子（引导注册，见 MSG_*_TRIAL）；普通用户提示可用自定义 LLM 或次日再来。
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
        # 用户维度：优先 24h（日配额），其次 1h（防突刺）
        user_blocked = None
        if limit > 0:
            d_used, d_lim, d_reset = _window_status(f"pu:{uid}:d", limit, win_d, now)
            if d_used >= limit:
                user_blocked = ("今天", d_used, d_lim, d_reset)
            else:
                h_used, h_lim, h_reset = _window_status(f"pu:{uid}:h", limit, win_h, now)
                if h_used >= limit:
                    user_blocked = ("本小时", h_used, h_lim, h_reset)
        # IP 维度（仅测试用户，清 localStorage 即换新身份需不可自选的锚）
        ip_blocked = None
        if is_test and ip_limit > 0 and ip != "direct":
            d_used, d_lim, d_reset = _window_status(f"pi:{ip}:d", ip_limit, win_d, now)
            if d_used >= ip_limit:
                ip_blocked = ("今天", d_used, d_lim, d_reset)
            else:
                h_used, h_lim, h_reset = _window_status(f"pi:{ip}:h", ip_limit, win_h, now)
                if h_used >= ip_limit:
                    ip_blocked = ("本小时", h_used, h_lim, h_reset)
        # 测试用户优先走 IP 转化钩子（注册即可继续，做题记录全保留）
        if is_test and ip_blocked:
            label, used, lim, reset = ip_blocked
            raise QuotaExceeded(
                MSG_PROBLEM_LIMIT_TRIAL, reset_in=int(reset),
                used=used, limit=lim, window_label=label)
        if user_blocked:
            label, used, lim, reset = user_blocked
            detail = MSG_PROBLEM_LIMIT.format(
                used=used, limit=lim, window_label=label,
                reset_min=max(1, int(reset // 60)))
            raise QuotaExceeded(
                detail, reset_in=int(reset), used=used, limit=lim,
                window_label=label, can_use_custom_llm=True)
        # 全部通过 → 扣减（先查后扣，持锁内原子，不产生半扣状态）
        if limit > 0:
            _windows.setdefault(f"pu:{uid}:h", deque()).append(now)
            _windows.setdefault(f"pu:{uid}:d", deque()).append(now)
        if is_test and ip_limit > 0 and ip != "direct":
            _windows.setdefault(f"pi:{ip}:h", deque()).append(now)
            _windows.setdefault(f"pi:{ip}:d", deque()).append(now)


def check_chat_ask(request: Request | None, uid: str, problem_id, is_test: bool = False) -> None:
    """每题导师提问配额（仅 chat/stream 调用；problem_id 为空=未绑题，不计量）。"""
    if not problem_id or not _enabled() or _custom_llm_active():
        return
    limit, ip_limit, _ = _limits("CTA_QUOTA_CHAT_USER", 20, "CTA_QUOTA_CHAT_IP", 20)
    if limit <= 0 and ip_limit <= 0:
        return
    pid = str(problem_id)
    ip = _client_ip(request)
    msg = MSG_CHAT_LIMIT_TRIAL if is_test else MSG_CHAT_LIMIT
    with _lock:
        _prune_locked(time.monotonic())
        if limit > 0:
            _take_counter(f"cu:{uid}:{pid}", limit, msg)
        if is_test and ip_limit > 0 and ip != "direct":
            _take_counter(f"ci:{ip}:{pid}", ip_limit, msg)


def check_trace_followup(request: Request | None, uid: str, problem_id, is_test: bool = False) -> None:
    """每题轨迹追问配额（仅 analyze 系列带 message 的追问；首轮结构化分析不计）。"""
    if not problem_id or not _enabled() or _custom_llm_active():
        return
    limit, ip_limit, _ = _limits("CTA_QUOTA_TRACE_USER", 20, "CTA_QUOTA_TRACE_IP", 20)
    if limit <= 0 and ip_limit <= 0:
        return
    pid = str(problem_id)
    ip = _client_ip(request)
    msg = MSG_TRACE_LIMIT_TRIAL if is_test else MSG_TRACE_LIMIT
    with _lock:
        _prune_locked(time.monotonic())
        if limit > 0:
            _take_counter(f"tu:{uid}:{pid}", limit, msg)
        if is_test and ip_limit > 0 and ip != "direct":
            _take_counter(f"ti:{ip}:{pid}", ip_limit, msg)
