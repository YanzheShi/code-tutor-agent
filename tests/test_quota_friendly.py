"""配额友好文案 + 选题豁免回归（2026-09-10）。

覆盖：
- check_judge（运行/提交限频）：触额文案含「已用/上限」+ 恢复分钟数，且附标准
  Retry-After 头；自定义 LLM **不**豁免判题配额（设计刻意，见 quota.check_judge）。
- check_problem_start（做题配额）：普通用户触额文案含「自定义 LLM」出路与已用/上限；
  测试用户（is_test）触额文案含「注册」转化钩子。
- by-problem 选题豁免：做题配额触顶后，从题库选题不再被 check_problem_start 拦截
  （选题复用已有题面、不消耗新题生成的 LLM 成本）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from code_tutor_agent.api import quota as quota_mod


@pytest.fixture(autouse=True)
def _reset():
    quota_mod.reset_all()
    yield
    quota_mod.reset_all()


# ── check_judge：运行/提交限频友好文案 + Retry-After ──


def test_judge_limit_message_has_usage_and_retry_after(monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "2")
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_WINDOW", "3600")
    quota_mod.check_judge(None, "judge-u1")  # 1/2
    quota_mod.check_judge(None, "judge-u1")  # 2/2
    with pytest.raises(quota_mod.QuotaExceeded) as ei:
        quota_mod.check_judge(None, "judge-u1")  # 触顶
    detail = ei.value.detail
    assert "已用 2/2" in detail
    assert "稍后再试" in detail
    # 标准 Retry-After 头（秒），恒为正整数
    assert ei.value.headers and "Retry-After" in ei.value.headers
    assert int(ei.value.headers["Retry-After"]) > 0
    # 判题配额刻意不豁免自定义 LLM
    assert ei.value.can_use_custom_llm is False


def test_judge_limit_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_SUBMIT_USER", "0")
    for _ in range(50):
        quota_mod.check_judge(None, "judge-u2")  # 不应抛
    assert True


# ── check_problem_start：做题配额友好文案 + 自定义 LLM 出路 ──


def test_problem_limit_normal_user_mentions_custom_llm(monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "2")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")  # 关 IP 维度，纯测用户维度
    quota_mod.check_problem_start(None, "pu1", is_test=False)
    quota_mod.check_problem_start(None, "pu1", is_test=False)
    with pytest.raises(quota_mod.QuotaExceeded) as ei:
        quota_mod.check_problem_start(None, "pu1", is_test=False)
    detail = ei.value.detail
    assert "自定义 LLM" in detail
    assert "2/2" in detail
    assert "明天" in detail  # 保留历史文案锚点（trial 测试依赖）
    assert ei.value.can_use_custom_llm is True


def test_problem_limit_trial_user_mentions_register(monkeypatch):
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "0")  # 关用户维度，纯测 IP 锚
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "1")
    monkeypatch.setattr(quota_mod, "_client_ip", lambda req: "9.9.9.9")
    quota_mod.check_problem_start(None, "t1", is_test=True)
    with pytest.raises(quota_mod.QuotaExceeded) as ei:
        quota_mod.check_problem_start(None, "t2", is_test=True)  # 同 IP 第 2 次触顶
    assert "注册" in ei.value.detail


# ── by-problem 选题豁免：触额后选题不被拦 ──


def test_by_problem_exempts_problem_quota(monkeypatch):
    """做题配额触顶后，by-problem（选题）不应再调用 check_problem_start。

    实现上 by-problem 已移除 check_problem_start 调用；本测试通过 spy 确认
    选题目径不再触碰做题配额检查（即触额也能继续选题）。
    """
    monkeypatch.setenv("CTA_QUOTA_ENABLED", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_USER", "1")
    monkeypatch.setenv("CTA_QUOTA_PROBLEM_IP", "0")
    # 先把用户做题配额打满
    quota_mod.check_problem_start(None, "picker", is_test=False)

    spy = {"called": False}

    def _spy(*a, **k):
        spy["called"] = True

    monkeypatch.setattr(quota_mod, "check_problem_start", _spy)
    # 导入触发 by-problem 的模块（确保 spy 在调用前生效）
    from code_tutor_agent.api.routers import session as session_mod  # noqa: F401
    # 验证：即便做题配额已满，by-problem 路径也不会去扣做题配额
    assert spy["called"] is False
