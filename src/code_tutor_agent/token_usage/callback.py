"""零侵入 LLM 用量回调。

关键实现:LangChain 的 ``on_llm_end`` 调用时**只传 response,不传 metadata**
(langchain_core.language_models.chat_models 839/1192 行),而 ``on_llm_start``
调用时才会带 ``metadata=self.metadata``(manager.py:1412)。因此不能指望在
``on_llm_end`` 的 kwargs 里取到归因信息,必须在 ``on_llm_start`` 时按
``run_id`` 缓存 metadata,``on_llm_end`` 再按 ``run_id`` 取回。

采集维度(两者在 on_llm_start 时都已在 metadata 里):
- purpose / model_alias / model_name ← get_llm() 构造参数 metadata 注入
  (模型实例属性,经 bind_tools / with_structured_output 后仍存活)
- session_id / mode / topic / difficulty / problem_id ← build_run_config 注入

线程模型:全局单例 handler,``_run_meta`` 用锁保护(回调可并发)。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from code_tutor_agent.token_usage.cost import cost_calculator
from code_tutor_agent.token_usage.sink import get_token_sink

logger = logging.getLogger(__name__)


class TokenUsageCallbackHandler(BaseCallbackHandler):
    """读取 LLM 用量并旁路落库(经 start→end run_id 映射归因)。"""

    # 不阻断主流程:任何异常只记日志
    raise_error = False

    def __init__(self) -> None:
        # run_id → {"meta": metadata, "t0": 起始时间}:在 on_llm_start 写入,
        # on_llm_end 消费后清理
        self._run_meta: dict[str, dict] = {}
        self._meta_lock = threading.Lock()

    def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        """LangChain 在此处携带完整 metadata(含 llm 级 + graph 级),
        按 run_id 缓存(连同起始时间),供 on_llm_end 归因。"""
        if run_id is None or not metadata:
            return
        with self._meta_lock:
            self._run_meta[str(run_id)] = {"meta": dict(metadata), "t0": time.monotonic()}

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._handle(response, run_id=run_id, extra=kwargs)
        except Exception as exc:  # pragma: no cover - 采集层绝不抛回主流程
            logger.debug("[token] on_llm_end skipped: %s", exc)

    # ── 内部 ──
    def _handle(self, response: Any, *, run_id: Any, extra: dict) -> None:
        # 1) 取 usage_metadata(优先 message,回退 llm_output / kwargs)
        um = self._extract_usage(response, extra)
        if not um:
            return

        # 2) 归因:优先取 on_llm_start 缓存的 metadata;兜底读 kwargs(兼容异常路径)
        rid = str(run_id) if run_id is not None else ""
        meta = {}
        t0 = None
        if rid:
            with self._meta_lock:
                start = self._run_meta.pop(rid, {}) or {}
                meta.update(start.get("meta", {}) or {})
                t0 = start.get("t0")
        if isinstance(extra, dict):
            m = extra.get("metadata")
            if isinstance(m, dict):
                meta.update(m)

        purpose = meta.get("purpose") or "unknown"
        model_name = meta.get("model_name") or ""
        model_alias = meta.get("model_alias") or ""

        cache_creation_tokens, cache_read_tokens = _extract_cache_tokens(um)

        rec = {
            "session_id": meta.get("session_id", ""),
            "user_id": meta.get("user_id", "default"),
            "purpose": purpose,
            "model_alias": model_alias,
            "model_name": model_name,
            "mode": meta.get("mode", ""),
            "topic": meta.get("topic", ""),
            "difficulty": meta.get("difficulty", ""),
            "problem_id": int(meta.get("problem_id") or 0),
            "prompt_tokens": int(um.get("input_tokens", 0) or 0),
            "completion_tokens": int(um.get("output_tokens", 0) or 0),
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "total_tokens": int(um.get("total_tokens", 0) or 0),
            "cost": 0.0,
            "latency_ms": int((time.monotonic() - t0) * 1000) if t0 else 0,
            "run_id": rid,
            "ts_day": datetime.now().strftime("%Y-%m-%d"),
        }
        rec["cost"] = cost_calculator(model_name, rec)
        get_token_sink().enqueue(rec)

    @staticmethod
    def _extract_usage(response: Any, extra: dict) -> dict:
        """从 LLMResult 提取 usage_metadata,兼容多种来源。

        返回字段对齐 ``input_tokens / output_tokens / total_tokens``,
        并附带 ``input_token_details``(langchain-core ≥0.3 缓存信息只在这;
        旧的顶层 ``cache_*_input_tokens`` 键已废弃移除);取不到返回 {}。
        """
        # 路径 A:generation[0][0].message.usage_metadata
        try:
            gens = response.generations
            if gens and gens[0]:
                msg = gens[0][0].message
                um = getattr(msg, "usage_metadata", None)
                if um:
                    return dict(um)
        except (AttributeError, IndexError, TypeError):
            pass

        # 路径 B:response.llm_output.usage(OpenAI 兼容原始结构)
        try:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage")
            if usage:
                return _normalize_openai_usage(usage)
        except Exception:
            pass

        # 路径 C:kwargs 中可能直接带来 usage
        try:
            u = extra.get("usage") or extra.get("token_usage")
            if u:
                return _normalize_openai_usage(u)
        except Exception:
            pass

        return {}


def _extract_cache_tokens(um: dict) -> tuple[int, int]:
    """从 usage_metadata 提取缓存 token(cache_creation / cache_read)。

    优先读 ``input_token_details``(langchain-core ≥0.3 的标准位置,OpenAI
    ``prompt_tokens_details.cached_tokens`` 被映射到这里);兼容旧版顶层键。
    """
    details = um.get("input_token_details")
    if isinstance(details, dict):
        creation = int(details.get("cache_creation") or 0)
        read = int(details.get("cache_read") or 0)
    else:
        creation = read = 0
    # 旧版 langchain 兜底(顶层废弃键)
    if not read:
        read = int(um.get("cache_read_input_tokens", 0) or 0)
    if not creation:
        creation = int(um.get("cache_creation_input_tokens", 0) or 0)
    return creation, read


def _normalize_openai_usage(usage: Any) -> dict:
    """把 OpenAI 风格 usage 规整为 usage_metadata 字段名。

    缓存命中兼容两种厂商字段:
    - OpenAI 风格 ``prompt_tokens_details.cached_tokens``
    - DeepSeek 风格 ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
    """
    get = usage.get if isinstance(usage, dict) else getattr
    prompt_details = get("prompt_tokens_details", None) or {}
    if isinstance(prompt_details, dict):
        cached = (
            prompt_details.get("cached_tokens", 0)
            or prompt_details.get("prompt_cache_hit_tokens", 0)
            or 0
        )
    else:
        cached = 0
    # DeepSeek 风格:命中数直接放在 usage 顶层
    if not cached:
        cached = get("prompt_cache_hit_tokens", 0) or 0
    input_tokens = int(get("prompt_tokens", 0) or 0)
    output_tokens = int(get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": int(cached or 0),
        "total_tokens": int(get("total_tokens", 0) or input_tokens + output_tokens),
    }
