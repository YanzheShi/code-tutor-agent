"""主 LLM 撞 RPM/限流时的请求级降级 — 本次调用直接改用 ALT 备用 key 重发。

设计口径（2026-09-07，用户决策）：
- 只接入热点调用点（对话意图判定 / 自由对话 tool loop / 出题 / 判题），
  不做全用途包装。
- 触发条件：**限流类瞬时错误**（429 / rate limit / quota）才切；
  其它异常原样上抛——真 bug 不被降级掩盖。
- 用户自配 key（custom 模式，runtime_settings 有 override）不降级：
  平台不替用户买单（与「备用 key 消耗归平台」的成本口径一致）。
- secondary 未配置（LLM_MODEL_ALT / LLM_API_KEY_ALT 缺失）时静默不启用。
- 开关：LLM_FAILOVER=0 可整体关闭（默认开）。
- 归因：fallback 实例由 get_llm(purpose, alias="secondary") 构造，
  metadata.model_alias="secondary"，token_usage 天然按备用 key 归集。

输出配额（2026-11-06 线上事故）：
网关以 400 "max_tokens exceeds the limit of N" 拒绝超限请求时，**不换 key 重试**
（换 key 也过不了同一个网关的上限），而是：把 N 记进 config 的学习缓存 →
用同一用途重建模型、按 N 重发一次。后续 get_llm 直接按 N 发，不再撞。

为什么不直接用 LangChain 的 with_fallbacks()：它返回 RunnableWithFallbacks
包装对象，会丢掉 BaseChatModel 专属的 with_structured_output / bind_tools
（本项目大量使用），所以只能在做调用的地方包一层。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

from code_tutor_agent.config import get_llm, note_output_token_limit

logger = logging.getLogger(__name__)

# 命中任一标记（lowercase 匹配）才视为限流类错误
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "tpm",
    "rpm",
    "requests per minute",
    "tokens per minute",
)


def is_rate_limit_error(exc: BaseException) -> bool:
    """判断异常是否为限流类瞬时错误（按错误文本匹配，跨网关通用）。"""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


# 网关对输出配额的拒绝，如 "max_tokens exceeds the limit of 4096"
_OUTPUT_LIMIT_RE = re.compile(r"max_tokens exceeds the limit of (\d+)", re.IGNORECASE)


def output_token_cap_from_error(exc: BaseException) -> int | None:
    """识别输出配额 400，返回网关声明的上限；其它错误返回 None。"""
    m = _OUTPUT_LIMIT_RE.search(f"{type(exc).__name__}: {exc}")
    return int(m.group(1)) if m else None


def _llm_identity(llm: Any) -> tuple[str, str]:
    """(base_url, model) 用于学习缓存；兼容 ChatOpenAI 与测试假对象。"""
    meta = getattr(llm, "metadata", None) or {}
    base = getattr(llm, "openai_api_base", None) or meta.get("base_url") or ""
    model = meta.get("model_name") or getattr(llm, "model_name", None) or ""
    return str(base), str(model)


def _m_record(name: str, value: float = 1.0) -> None:
    """监控埋点（非侵入：失败静默）。"""
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        get_registry().record(name, value)
    except Exception:
        pass


def _m_streak(ok: bool) -> None:
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        get_registry().record_streak("llm_fail", ok)
    except Exception:
        pass


def failover_enabled() -> bool:
    """降级开关：LLM_FAILOVER=0 关闭；ALT 模型/key 未配置时视为不可用。"""
    if os.getenv("LLM_FAILOVER", "1") != "1":
        return False
    return bool(os.getenv("LLM_MODEL_ALT") and os.getenv("LLM_API_KEY_ALT"))


def invoke_with_failover(
    primary_llm: Any,
    invoke_fn: Callable[[Any], Any],
    purpose: str = "",
) -> Any:
    """用 primary_llm 执行 invoke_fn(primary_llm)；限流类错误时改用 secondary 重发。

    Args:
        primary_llm: 主模型实例（已含用途参数/用户覆盖）。
        invoke_fn: 接收模型实例、执行一次完整调用的函数，如
            ``lambda m: m.with_structured_output(X).invoke(msgs)``。
            注意：bind_tools / with_structured_output 要在 invoke_fn 内做，
            这样 fallback 实例也能获得同样的包装。
        purpose: 用途名（日志 + fallback 构造 secondary 时复用用途参数）。

    Returns:
        invoke_fn 的返回值（主调用成功即主结果，限流降级即备用结果）。

    Raises:
        原样上抛非限流异常；限流但降级不可用/不允许时也原样上抛。
    """
    try:
        result = invoke_fn(primary_llm)
        _m_streak(True)
        return result
    except Exception as exc:
        cap = output_token_cap_from_error(exc)
        if cap:
            note_output_token_limit(*_llm_identity(primary_llm), cap)
            if not purpose:
                _m_streak(False)
                raise
            logger.warning(
                "LLM output budget rejected (purpose=%s): gateway caps max_tokens at %d "
                "— retrying once at the cap: %s",
                purpose, cap, exc,
            )
            _m_record("llm_token_cap_retry")
            try:
                result = invoke_fn(get_llm(purpose, max_tokens=cap))
            except Exception:
                _m_streak(False)
                raise
            _m_streak(True)
            return result
        if not is_rate_limit_error(exc):
            _m_streak(False)
            raise
        if not failover_enabled():
            logger.warning(
                "LLM rate-limited but failover disabled/unconfigured (purpose=%s): %s",
                purpose, exc,
            )
            _m_streak(False)
            raise
        # 用户自配 key 的请求不降级：平台备用 key 不替用户买单
        from code_tutor_agent.runtime_settings import get_llm_override

        if get_llm_override():
            logger.info(
                "LLM rate-limited but user custom key in effect — no failover (purpose=%s)",
                purpose,
            )
            _m_streak(False)
            raise
        logger.warning(
            "LLM rate-limited (purpose=%s) — failing over to secondary ALT key: %s",
            purpose, exc,
        )
        _m_record("llm_failover")
        fallback_llm = get_llm(purpose, alias="secondary")
        try:
            result = invoke_fn(fallback_llm)
            _m_streak(True)
            return result
        except Exception:
            _m_streak(False)
            raise
