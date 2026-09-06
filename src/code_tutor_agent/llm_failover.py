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

为什么不直接用 LangChain 的 with_fallbacks()：它返回 RunnableWithFallbacks
包装对象，会丢掉 BaseChatModel 专属的 with_structured_output / bind_tools
（本项目大量使用），所以只能在做调用的地方包一层。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from code_tutor_agent.config import get_llm

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
        return invoke_fn(primary_llm)
    except Exception as exc:
        if not is_rate_limit_error(exc):
            raise
        if not failover_enabled():
            logger.warning(
                "LLM rate-limited but failover disabled/unconfigured (purpose=%s): %s",
                purpose, exc,
            )
            raise
        # 用户自配 key 的请求不降级：平台备用 key 不替用户买单
        from code_tutor_agent.runtime_settings import get_llm_override

        if get_llm_override():
            logger.info(
                "LLM rate-limited but user custom key in effect — no failover (purpose=%s)",
                purpose,
            )
            raise
        logger.warning(
            "LLM rate-limited (purpose=%s) — failing over to secondary ALT key: %s",
            purpose, exc,
        )
        fallback_llm = get_llm(purpose, alias="secondary")
        return invoke_fn(fallback_llm)
