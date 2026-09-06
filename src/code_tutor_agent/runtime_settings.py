"""请求级 LLM 覆盖（用户自定义 API key / base URL / model）。

链路：
    HTTP 请求进入 → main.py 中间件按 Bearer token 解出 user_id →
    读 user_settings 表（带内存缓存）→ 写入本模块 ContextVar →
    config.get_llm() 优先采用覆盖值 → 请求结束 reset。

- 未设置 / 用户用默认模式 → 回退服务器 .env 配置（LLM_CONFIGS）。
- asyncio.to_thread 会拷贝当前 contextvars，判题/出题在线程池里跑也能拿到覆盖。
- 直接 spawn 的后台线程（如错误模式分析 fire_and_forget）不继承请求上下文，
  自动回退服务器默认 —— 这是特性不是 bug：后台分析不消耗用户自己的 key。
"""
from __future__ import annotations

import contextvars
from typing import Any

# None = 无覆盖（服务器默认）；dict = {"model", "base_url", "api_key"} 非空子集
llm_override_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "llm_override", default=None,
)


def set_llm_override(cfg: dict[str, str] | None) -> contextvars.Token:
    """设置当前请求的 LLM 覆盖配置，返回 token 供请求结束时 reset。"""
    return llm_override_ctx.set(cfg)


def get_llm_override() -> dict[str, str] | None:
    """get_llm() 调用：返回当前覆盖配置（可能为 None）。"""
    return llm_override_ctx.get()


def build_override(model: str | None, base_url: str | None, api_key: str | None) -> dict[str, str]:
    """从设置行组装覆盖 dict，空值字段剔除（保持 None 语义：该字段用服务器默认）。"""
    cfg: dict[str, Any] = {
        "model": (model or "").strip(),
        "base_url": (base_url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
    }
    return {k: v for k, v in cfg.items() if v}
