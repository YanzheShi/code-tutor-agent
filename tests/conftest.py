"""全局测试配置。

Rate-limit 桶隔离（防滥用改造配套）：auth 的 IP 限流桶是模块级内存态，
整包跑时跨测试累积会把后续走 TestClient 注册的测试顶到 429。
每条测试前后自动清桶；限流行为本身由 test_auth_multitenant 专项覆盖。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    from code_tutor_agent.api import auth as auth_mod

    auth_mod._RATE_BUCKETS.clear()
    yield
    auth_mod._RATE_BUCKETS.clear()
