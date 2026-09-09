"""用户代码输入闸门测试（2026-09-09，10KB / 300 行三道闸门之后端部分）。

覆盖：
- check_code_limits 边界：恰好 10KB / 300 行放行，超 1 字节 / 1 行拒绝；
  UTF-8 多字节按字节数计（不是字符数）；错误文案含上限信息；
- enforce_code_limits：路由层抛 HTTPException(413)；
- run / submit 路由接线：413 在限频与 graph 之前抛出（不消耗配额、不碰 graph）。

不依赖 LLM / HTTP 服务 / Judge0。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from code_tutor_agent.api.validation import (
    CodeTooLargeError,
    check_code_limits,
    enforce_code_limits,
)
from code_tutor_agent.config import MAX_CODE_BYTES, MAX_CODE_LINES
from code_tutor_agent.schemas.api import RunCodeRequest, SubmitRequest


def _mk_lines(n: int) -> str:
    """生成恰好 n 行的代码（无尾换行）：backend count('\\n')+1 == frontend split('\\n').length == n。"""
    return "\n".join(f"x{i} = {i}" for i in range(n))


# ── check_code_limits：字节边界 ──────────────────────────────────


class TestByteLimit:
    def test_exactly_at_limit_passes(self):
        code = "a" * MAX_CODE_BYTES
        check_code_limits(code)  # 不抛

    def test_one_byte_over_rejected(self):
        code = "a" * (MAX_CODE_BYTES + 1)
        with pytest.raises(CodeTooLargeError) as ei:
            check_code_limits(code)
        assert "字节" in ei.value.args[0]
        assert str(MAX_CODE_BYTES) in ei.value.args[0]

    def test_multibyte_counted_as_bytes_not_chars(self):
        # 3277 个「中」= 9831 字节 < 10KB，字符数也是 3277 —— 二者都不过界，放行
        n = MAX_CODE_BYTES // 3  # 每字 3 字节，必然 ≤ 上限
        check_code_limits("中" * n)
        # 再多 1 个字即超限（3*(n+1) > 10KB）——验证按字节而非字符计数
        with pytest.raises(CodeTooLargeError):
            check_code_limits("中" * (n + 1))

    def test_empty_code_passes(self):
        check_code_limits("")


# ── check_code_limits：行数边界（口径与前端一致：split('\n').length）──


class TestLineLimit:
    def test_exactly_at_limit_passes(self):
        # 口径统一验证：后端 count('\n')+1 与前端 split('\n').length 相等
        code = _mk_lines(MAX_CODE_LINES)
        n_backend = code.count("\n") + 1
        n_frontend = len(code.split("\n"))
        assert n_backend == n_frontend == MAX_CODE_LINES
        check_code_limits(code)  # 不抛

    def test_one_line_over_rejected(self):
        code = _mk_lines(MAX_CODE_LINES + 1)
        with pytest.raises(CodeTooLargeError) as ei:
            check_code_limits(code)
        assert "行" in ei.value.args[0]
        assert str(MAX_CODE_LINES) in ei.value.args[0]

    def test_few_lines_but_bytes_over_rejected_by_bytes(self):
        # 行数很少但单行巨长：字节闸门优先触发
        code = "a" * (MAX_CODE_BYTES + 1)  # 1 行
        with pytest.raises(CodeTooLargeError) as ei:
            check_code_limits(code)
        assert "字节" in ei.value.args[0]


# ── enforce_code_limits：HTTP 413 ────────────────────────────────


class TestEnforceCodeLimits:
    def test_oversized_raises_413_with_readable_detail(self):
        code = _mk_lines(MAX_CODE_LINES + 1)
        with pytest.raises(HTTPException) as ei:
            enforce_code_limits(code)
        assert ei.value.status_code == 413
        assert "上限" in ei.value.detail

    def test_normal_code_no_exception(self):
        enforce_code_limits(_mk_lines(50))  # 不抛


# ── 路由接线：413 在限频 / graph 之前抛出 ────────────────────────


class TestRouterWiring:
    def _patch_entry(self, monkeypatch):
        """把 run/submit 入口的越权校验与判题限频替换为直通桩。

        越权校验在闸门之前（直通即可）；判题限频在闸门之后——
        若 413 生效，限频桩不应被触达（超限拒收不应消耗判题配额）。
        """
        touched = {"quota": False}

        from code_tutor_agent.api import quota as quota_mod

        def _spy_quota(req, uid):
            touched["quota"] = True

        monkeypatch.setattr("code_tutor_agent.api.routers.run.get_session_owner", lambda sid: None)
        monkeypatch.setattr("code_tutor_agent.api.routers.session._require_owner", lambda sid, current: None)
        monkeypatch.setattr(quota_mod, "check_judge", _spy_quota)
        return touched

    def test_run_rejects_oversized_before_quota_and_graph(self, monkeypatch):
        from code_tutor_agent.api.routers import run as run_mod

        touched = self._patch_entry(monkeypatch)
        body = RunCodeRequest(code=_mk_lines(MAX_CODE_LINES + 1))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(run_mod.run_code("s1", body, current={"id": 1}, request=None))
        assert ei.value.status_code == 413
        assert touched["quota"] is False  # 限频未被触达（拒收不烧配额）

    def test_submit_rejects_oversized_before_quota_and_graph(self, monkeypatch):
        from code_tutor_agent.api.routers import session as session_mod

        touched = self._patch_entry(monkeypatch)
        body = SubmitRequest(code="a" * (MAX_CODE_BYTES + 1))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(session_mod.submit_code("s1", body, current={"id": 1}, request=None))
        assert ei.value.status_code == 413
        assert touched["quota"] is False

    def test_run_rejects_too_many_lines(self, monkeypatch):
        from code_tutor_agent.api.routers import run as run_mod

        self._patch_entry(monkeypatch)
        body = RunCodeRequest(code=_mk_lines(MAX_CODE_LINES + 5))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(run_mod.run_code("s1", body, current={"id": 1}, request=None))
        assert ei.value.status_code == 413
        assert "行" in ei.value.detail
