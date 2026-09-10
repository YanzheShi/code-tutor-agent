"""安全加固回归测试（2026-09-08 审计修复 F-01/F-02/F-03/F-04/F-09）。

不依赖 LLM / HTTP 服务 / Judge0。覆盖：
- F-04 业务配额：做题滚动窗口、每题提问、每题追问、custom-LLM 豁免、总闸关闭
- F-01/P0 沙箱 fail-closed：CTA_SANDBOX_ALLOW_LOCAL_FALLBACK=0 时 Judge0 失败不降级本地
- F-02 SSRF：base_url 校验（开发档拦 metadata、生产档拦私网 + localhost）
- F-03 XFF：默认不信任 X-Forwarded-For；显式开启后才信任
- F-09 重置码防爆破：错误达上限作废验证码、新码签发清零
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from code_tutor_agent.api import auth as auth_mod
from code_tutor_agent.api import quota as quota_mod
from code_tutor_agent.api.routers import settings as settings_mod
from code_tutor_agent.sandbox import runner as runner_mod


# ── F-04：业务配额 ──────────────────────────────────────────────


@pytest.fixture()
def quota_on(monkeypatch):
    """开启配额总闸 + 每用例后清状态。"""
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    quota_mod.reset_all()
    yield
    quota_mod.reset_all()


class _FakeReq:
    """极简 Request 替身：_client_ip 只读 headers / client。"""

    def __init__(self, ip: str = "1.2.3.4", headers: dict | None = None):
        self.headers = headers or {}
        from types import SimpleNamespace

        self.client = SimpleNamespace(host=ip)


def test_problem_start_quota_blocks_after_limit(quota_on, monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")  # 只测用户维度
    req = _FakeReq()
    for _ in range(5):
        quota_mod.check_problem_start(req, "42")  # 不抛
    with pytest.raises(HTTPException) as ei:
        quota_mod.check_problem_start(req, "42")
    assert ei.value.status_code == 429
    assert "额度已用完" in ei.value.detail


def test_problem_start_quota_ip_dimension(quota_on, monkeypatch):
    """IP 维度仅对测试用户（体验账号）生效（2026-09-10 契约变更）。

    - 体验账号清 localStorage 即换新身份，IP 是不可自选的锚：同 IP 换 uid 仍受限；
    - 注册用户纯用户维度：校园网/NAT 多人同公网 IP 不再互相误杀（换 uid 各自计）。
    """
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "0")  # 只测 IP 维度
    # 显式恢复 IP 维度默认阈值：conftest load_dotenv 会带入 .env 的覆盖值，测试不依赖 .env 现状
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "5")
    req = _FakeReq(ip="9.9.9.9")
    # 测试用户：同 IP 换 uid 仍受 IP 限额约束
    for _ in range(5):
        quota_mod.check_problem_start(req, "1", is_test=True)
    with pytest.raises(HTTPException):
        quota_mod.check_problem_start(req, "2", is_test=True)  # 换用户仍受同 IP 限额约束

    # 注册用户：IP 维度不生效（换 uid 各自独立，同 IP 不互相影响）
    for uid in ("11", "12", "13"):
        quota_mod.check_problem_start(req, uid, is_test=False)  # 不抛 = 通过


def test_problem_start_quota_rolling_window_recovers(quota_on, monkeypatch):
    """滑动窗口：时间推过后额度恢复（滚动 24h 语义，非自然日）。"""
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")
    req = _FakeReq()
    for _ in range(5):
        quota_mod.check_problem_start(req, "7")
    # 手动把窗口内时间戳拨回 25 小时前 → 全部滑出窗口
    import time as _time

    with quota_mod._lock:
        for key, bucket in quota_mod._windows.items():
            if key.startswith("pu:7:"):
                quota_mod._windows[key] = type(bucket)(
                    t - 25 * 3600 for t in bucket
                )
    quota_mod.check_problem_start(req, "7")  # 不抛 = 额度已恢复
    del _time


def test_chat_ask_quota_per_problem(quota_on, monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_CHAT_IP", "0")
    req = _FakeReq()
    for _ in range(20):
        quota_mod.check_chat_ask(req, "42", 101)  # 不抛
    with pytest.raises(HTTPException) as ei:
        quota_mod.check_chat_ask(req, "42", 101)
    assert "提问次数已达上限" in ei.value.detail
    # 不同题独立计量
    quota_mod.check_chat_ask(req, "42", 202)


def test_chat_ask_skipped_without_problem(quota_on):
    """未绑题（出题对话阶段）不计每题提问配额。"""
    req = _FakeReq()
    for _ in range(30):
        quota_mod.check_chat_ask(req, "42", None)  # 不抛


def test_trace_followup_quota(quota_on, monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_TRACE_IP", "0")
    req = _FakeReq()
    for _ in range(20):
        quota_mod.check_trace_followup(req, "42", "p1")
    with pytest.raises(HTTPException) as ei:
        quota_mod.check_trace_followup(req, "42", "p1")
    assert "追问次数已达上限" in ei.value.detail


def test_quota_exempt_for_custom_llm(quota_on, monkeypatch):
    """自带 API key（custom LLM 覆盖生效中）不受任何配额限制。"""
    from code_tutor_agent import runtime_settings

    token = runtime_settings.set_llm_override({"model": "m", "base_url": "https://x", "api_key": "k"})
    try:
        req = _FakeReq()
        for _ in range(10):
            quota_mod.check_problem_start(req, "42")  # 不抛
            quota_mod.check_chat_ask(req, "42", 1)
            quota_mod.check_trace_followup(req, "42", "p1")
    finally:
        runtime_settings.llm_override_ctx.reset(token)


def test_quota_disabled_by_default_in_tests(quota_on, monkeypatch):
    """总闸 ≤0 = 全部放行（conftest 依赖此行为隔离集成测试）。"""
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "0")
    req = _FakeReq()
    for _ in range(10):
        quota_mod.check_problem_start(req, "42")
        quota_mod.check_chat_ask(req, "42", 1)


# ── F-04 补充：判题限频（submit/run，不豁免自带 key 用户）─────────


def test_judge_quota_blocks_after_limit(quota_on, monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "30")
    req = _FakeReq()
    for _ in range(30):
        quota_mod.check_judge(req, "42")  # 不抛
    with pytest.raises(HTTPException) as ei:
        quota_mod.check_judge(req, "42")
    assert "提交/运行太频繁" in ei.value.detail


def test_judge_quota_rolling_window_recovers(quota_on, monkeypatch):
    """滑动窗口：时间推过后恢复。"""
    from collections import deque

    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "30")
    req = _FakeReq()
    for _ in range(30):
        quota_mod.check_judge(req, "42")
    with quota_mod._lock:
        quota_mod._windows["ju:42"] = deque(t - 7200 for t in quota_mod._windows["ju:42"])
    quota_mod.check_judge(req, "42")  # 不抛 = 已恢复


def test_judge_quota_not_exempt_for_custom_llm(quota_on, monkeypatch):
    """自带 API key 用户**不豁免**判题限频（判题 CPU 是服务器资源）。"""
    from code_tutor_agent import runtime_settings

    # 显式设定限额，不依赖默认值（默认值调整不应影响本测试）
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "20")
    token = runtime_settings.set_llm_override({"model": "m", "base_url": "https://x", "api_key": "k"})
    try:
        req = _FakeReq()
        for _ in range(20):
            quota_mod.check_judge(req, "42")
        with pytest.raises(HTTPException):
            quota_mod.check_judge(req, "42")
    finally:
        runtime_settings.llm_override_ctx.reset(token)


def test_judge_quota_disabled(quota_on, monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "0")
    req = _FakeReq()
    for _ in range(35):
        quota_mod.check_judge(req, "42")  # 不抛


# ── F-04 补充：并发护栏排队上限 ─────────────────────────────────


def test_concurrency_queue_cap(monkeypatch):
    """排队+执行总数达到 _MAX_CONCURRENCY + MAX_CONCURRENCY_QUEUE 时 429。"""
    import asyncio
    import threading

    from code_tutor_agent.api import deps

    monkeypatch.setattr(deps, "_MAX_CONCURRENCY", 1)
    monkeypatch.setenv("MAX_CONCURRENCY_QUEUE", "1")
    deps._active_count = 0

    release = threading.Event()

    def holder():
        # 同步阻塞（to_thread 线程内），真实占住信号量槽位
        release.wait(timeout=5)

    async def scenario():
        t1 = asyncio.create_task(deps.run_with_concurrency_limit(holder))  # 占槽
        t2 = asyncio.create_task(deps.run_with_concurrency_limit(holder))  # 排队
        # 等 t1/t2 都进入护栏（_active_count == 2）
        for _ in range(200):
            if deps._active_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert deps._active_count >= 2
        with pytest.raises(HTTPException) as ei:
            await deps.run_with_concurrency_limit(holder)  # 第三个 → 429
        assert "判题繁忙" in ei.value.detail
        release.set()
        await asyncio.gather(t1, t2)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        deps._active_count = 0


# ── F-01/P0：沙箱 fail-closed ───────────────────────────────────


def test_local_fallback_default_on():
    """默认（本地开发）保留本地降级。"""
    import os

    old = os.environ.pop("CTA_SANDBOX_ALLOW_LOCAL_FALLBACK", None)
    try:
        assert runner_mod._allow_local_fallback() is True
    finally:
        if old is not None:
            os.environ["CTA_SANDBOX_ALLOW_LOCAL_FALLBACK"] = old


def test_sandbox_fail_closed_when_fallback_disabled(monkeypatch):
    """CTA_SANDBOX_ALLOW_LOCAL_FALLBACK=0 + Judge0 不可达 → Judge Error，不落本地执行。"""
    monkeypatch.setenv("CTA_SANDBOX_ALLOW_LOCAL_FALLBACK", "0")
    monkeypatch.setenv("JUDGE0_URL", "http://127.0.0.1:1")  # 必然连不上的端口

    def _boom(*a, **k):
        raise RuntimeError("judge0 down")

    monkeypatch.setattr(
        "code_tutor_agent.sandbox.judge0_client.submit_test_cases", _boom
    )
    # 记录 subprocess.run 是否被调用（绝不该走到本地执行）
    called = {"n": 0}
    real_run = runner_mod.subprocess.run

    def _spy(*a, **k):
        called["n"] += 1
        return real_run(*a, **k)

    monkeypatch.setattr(runner_mod.subprocess, "run", _spy)

    results = runner_mod.run_solution(
        "class Solution:\n    def x(self, a):\n        return a",
        [{"input_args": ["1"], "expected_output": "1"}],
        function_signature=None,
    )
    assert len(results) == 1
    assert results[0].status == "Judge Error"
    assert called["n"] == 0  # 不可信代码未进入本地子进程


def test_sandbox_local_fallback_still_works_when_enabled(monkeypatch):
    """默认开启时，Judge0 失败仍降级本地执行（本地开发行为不变）。"""
    monkeypatch.setenv("CTA_SANDBOX_ALLOW_LOCAL_FALLBACK", "1")
    monkeypatch.delenv("JUDGE0_URL", raising=False)

    results = runner_mod.run_solution(
        "class Solution:\n    def twoSum(self, a, b):\n        return a + b",
        [{"input_args": ["1", "2"], "expected_output": "3"}],
        function_signature=None,
    )
    assert results[0].status == "Passed"


# ── F-02：SSRF 校验 ─────────────────────────────────────────────


def test_ssrf_dev_blocks_metadata(monkeypatch):
    monkeypatch.delenv("CTA_ENV", raising=False)
    monkeypatch.delenv("CTA_SSRF_BLOCK_PRIVATE", raising=False)
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://169.254.169.254/latest")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://metadata.google.internal/v1")


def test_ssrf_dev_allows_localhost(monkeypatch):
    """开发档放行本机网关（Ollama 场景）。"""
    monkeypatch.delenv("CTA_ENV", raising=False)
    monkeypatch.delenv("CTA_SSRF_BLOCK_PRIVATE", raising=False)
    settings_mod._validate_base_url("http://localhost:11434/v1")
    settings_mod._validate_base_url("https://api.deepseek.com/v1")


def test_ssrf_rejects_bad_scheme(monkeypatch):
    monkeypatch.delenv("CTA_ENV", raising=False)
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("ftp://example.com")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("file:///etc/passwd")


def test_ssrf_strict_blocks_private(monkeypatch):
    monkeypatch.setenv("CTA_ENV", "production")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://localhost:11434/v1")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://127.0.0.1:8080")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://10.0.0.5:8000")
    with pytest.raises(HTTPException):
        settings_mod._validate_base_url("http://169.254.169.254/latest")
    # 公网地址放行
    settings_mod._validate_base_url("https://api.deepseek.com/v1")


# ── F-03：XFF 信任 ──────────────────────────────────────────────


def test_xff_untrusted_by_default(monkeypatch):
    monkeypatch.delenv("CTA_TRUST_PROXY_HEADERS", raising=False)
    req = _FakeReq(ip="5.6.7.8", headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"})
    assert auth_mod._client_ip(req) == "5.6.7.8"


def test_xff_trusted_when_enabled(monkeypatch):
    monkeypatch.setenv("CTA_TRUST_PROXY_HEADERS", "1")
    req = _FakeReq(ip="5.6.7.8", headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"})
    assert auth_mod._client_ip(req) == "1.1.1.1"


def test_xff_none_request(monkeypatch):
    monkeypatch.setenv("CTA_TRUST_PROXY_HEADERS", "1")
    assert auth_mod._client_ip(None) == "direct"


# ── F-09：重置验证码防爆破 ──────────────────────────────────────


def test_reset_code_invalidated_after_max_attempts(monkeypatch):
    monkeypatch.setenv("CTA_RESET_MAX_ATTEMPTS", "5")
    auth_mod._RESET_ATTEMPTS.clear()
    invalidated: list[str] = []
    monkeypatch.setattr(
        "code_tutor_agent.db.database.invalidate_password_reset_code",
        lambda email: invalidated.append(email),
    )
    for _ in range(5):
        auth_mod._record_reset_fail("victim@example.com")
    assert invalidated == ["victim@example.com"]  # 恰好在第 5 次触发
    auth_mod._RESET_ATTEMPTS.clear()


def test_reset_code_counter_cleared(monkeypatch):
    monkeypatch.setenv("CTA_RESET_MAX_ATTEMPTS", "5")
    auth_mod._RESET_ATTEMPTS.clear()
    invalidated: list[str] = []
    monkeypatch.setattr(
        "code_tutor_agent.db.database.invalidate_password_reset_code",
        lambda email: invalidated.append(email),
    )
    auth_mod._record_reset_fail("a@example.com")
    auth_mod._record_reset_fail("a@example.com")
    auth_mod._clear_reset_fail("a@example.com")
    auth_mod._record_reset_fail("a@example.com")
    auth_mod._record_reset_fail("a@example.com")
    assert invalidated == []  # 清零后未达上限
    auth_mod._RESET_ATTEMPTS.clear()
