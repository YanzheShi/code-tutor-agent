"""tests/integration/ 下所有用例自动打 `integration` 标记。

这些是真实端到端集成测试，会真调 LLM / 沙箱（单题出题实测约 3 分钟），
用于日常快速回归时跳过：

    uv run pytest -m "not integration"        # 只跑快的（单元/接口）
    uv run pytest -m integration              # 只跑集成
    uv run pytest                             # 全跑（CI / 完整验证）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if path is None:
            continue
        if _HERE in Path(str(path)).resolve().parents:
            item.add_marker(pytest.mark.integration)


sys.path.insert(0, str(_HERE))

import os  # noqa: E402

import _agent_helpers  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _inject_admin_env():
    """集成测试显式注入引导管理员凭据，使 ensure_bootstrap_admin 可创建 admin。

    auth.py 已改为 env-only（无源码默认值），故测试环境必须提供
    CTA_ADMIN_EMAIL / CTA_ADMIN_PASSWORD；值与 _agent_helpers 的登录账号保持一致。

    注意：不能在这里请求 function 级 monkeypatch（ScopeMismatch，pytest 9 实测
    54 条集成测试 setup 全 ERROR，2026-09-09）——session 级 fixture 直接
    os.environ 写入并在会话结束后还原。

    兜底值必须与 _agent_helpers 的登录兜底**同源**（CTA_TEST_EMAIL 未设时
    两处都取 534629255@qq.com / test123456），否则 bootstrap admin 建出来
    是 test-admin@example.com、自愈登录却拿 534629255@qq.com → 「邮箱或
    密码错误」全军覆没（2026-09-09 实测）。
    """
    _test_email = os.getenv("CTA_TEST_EMAIL", "534629255@qq.com")
    _test_password = os.getenv("CTA_TEST_PASSWORD", "test123456")
    saved = {k: os.environ.get(k) for k in ("CTA_ADMIN_EMAIL", "CTA_ADMIN_PASSWORD")}
    os.environ["CTA_ADMIN_EMAIL"] = _test_email
    os.environ["CTA_ADMIN_PASSWORD"] = _test_password
    yield
    for key, old in saved.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


@pytest.fixture(autouse=True)
def _reset_auth_token_cache():
    """每条测试后清 _agent_helpers 的 token 缓存。

    全局 conftest 每条测试后 TRUNCATE 全部表（含 users）：模块级 client 缓存的
    JWT sub 指向已被清掉的用户，下一条测试若继续用缓存 token 会 401
    「用户不存在或已删除」（2026-09-07 实测的级联）。admin 保活交给
    get_auth_token 的自愈登录（缺失时 ensure_bootstrap_admin + 重试），
    不在全局层注入用户——单元测试要断言「开局无 admin」。
    """
    yield
    _agent_helpers._token_cache.clear()
