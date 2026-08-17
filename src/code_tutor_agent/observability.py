"""集中式 LangSmith 可观测辅助模块（非侵入式）。

设计原则：
- 全部能力通过环境变量开关；未配置 ``LANGSMITH_API_KEY`` 时 tracing 自动关闭、
  feedback 静默跳过，主流程零影响。
- ``langsmith`` 采用**延迟导入**（函数内 import + 捕获异常），即使未安装或网络异常，
  本模块加载与主程序运行都不受影响。
- 基础追踪由 LangChain / LangGraph 的原生回调自动完成，本模块只负责：
  1) 统一的开关判断；
  2) 构造带会话元数据 / 标签 / run_name 的 graph.invoke config；
  3) 把客观 verdict 作为 feedback 回传。

环境变量（仅使用新名）：
- ``LANGSMITH_TRACING`` ：显式开关（true/false）
- ``LANGSMITH_API_KEY`` ：追踪开关与身份
- ``LANGSMITH_PROJECT`` ：项目名
- ``LANGSMITH_FEEDBACK``：反馈打分开关（默认 true）
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── 环境变量读取（兼容旧名） ────────────────────────────────────────────────
def _get(name: str) -> str | None:
    val = os.getenv(name)
    if val is not None and val.strip() != "":
        return val.strip()
    return None


def _tracing_explicitly_disabled() -> bool:
    val = _get("LANGSMITH_TRACING")
    if val is None:
        return False
    return val.strip().lower() in ("false", "0", "no", "off")


# ── 开关判断 ────────────────────────────────────────────────────────────────
def is_tracing_enabled() -> bool:
    """是否启用 LangSmith 追踪。

    仅当检测到 API key 且未被显式关闭时返回 True。
    """
    if _tracing_explicitly_disabled():
        return False
    return _get("LANGSMITH_API_KEY") is not None


def is_feedback_enabled() -> bool:
    """反馈打分是否启用（依赖 tracing 开启）。"""
    if not is_tracing_enabled():
        return False
    val = os.getenv("LANGSMITH_FEEDBACK")
    if val is None:
        return True
    return val.strip().lower() not in ("false", "0", "no", "off")


def _project_name() -> str:
    return _get("LANGSMITH_PROJECT") or "default"


# 模块加载时打印接线状态（便于启动期确认）
try:
    if is_tracing_enabled():
        logger.info(
            "[observability] LangSmith tracing ENABLED (project=%s)", _project_name()
        )
    else:
        logger.info(
            "[observability] LangSmith tracing DISABLED (no API key / explicitly off)"
        )
except Exception:  # pragma: no cover - 启动期绝不因可观测层崩溃
    pass


# ── Token 用量回调(零侵入采集;懒加载避免循环导入)──
_TOKEN_HANDLER = None


def _token_handler():
    """返回(缓存)TokenUsageCallbackHandler 单例,挂到 graph.invoke 的 config 上。"""
    global _TOKEN_HANDLER
    if _TOKEN_HANDLER is None:
        try:
            from code_tutor_agent.token_usage.callback import TokenUsageCallbackHandler

            _TOKEN_HANDLER = TokenUsageCallbackHandler()
        except Exception as exc:  # 采集层故障绝不影响主流程
            logger.warning("[observability] token handler init failed (ignored): %s", exc)
            return None
    return _TOKEN_HANDLER


# ── run config 构造 ──────────────────────────────────────────────────────────
def build_run_config(
    sid: str,
    *,
    mode: str | None = None,
    topic: str | None = None,
    difficulty: str | None = None,
    problem_id: int | None = None,
    run_name: str | None = None,
) -> dict:
    """构造 graph.invoke 用的 config。

    在现有 ``{"configurable": {"thread_id": sid}}`` 基础上，追加
    ``metadata`` / ``tags`` / ``run_name``，便于在 LangSmith 按会话维度筛查。
    未启用 tracing 时仍返回含 ``thread_id`` 的 config（不影响 checkpointer / 续跑）。
    """
    metadata: dict = {"session_id": sid, "app": "code-tutor-agent"}
    if mode:
        metadata["mode"] = mode
    if topic:
        metadata["topic"] = topic
    if difficulty:
        metadata["difficulty"] = difficulty
    if problem_id is not None:
        metadata["problem_id"] = problem_id

    tags = ["code-tutor"]
    if mode:
        tags.append(str(mode))

    config: dict = {
        "configurable": {"thread_id": sid},
        "metadata": metadata,
        "tags": tags,
    }
    handler = _token_handler()
    if handler is not None:
        config["callbacks"] = [handler]
    if run_name is not None:
        config["run_name"] = run_name
    return config


# ── 反馈打分 ────────────────────────────────────────────────────────────────
def get_langsmith_client():
    """延迟创建 LangSmith client；失败返回 None。"""
    try:
        from langsmith import Client

        return Client()
    except Exception as exc:  # 未安装 / 网络问题
        logger.warning("[observability] 无法创建 LangSmith client: %s", exc)
        return None


def record_verdict_feedback(
    run_id: str,
    verdict: str,
    *,
    session_id: str | None = None,
    hint_level: int | None = None,
    judge_cycle: int | None = None,
) -> None:
    """把客观 verdict 作为 feedback 回传到当次判题 trace。

    verdict → score 映射：``AC → 1``，其余（``WA``/``RE``/``TLE``/…）→ ``0``。
    全程非致命：任何异常仅记日志，不影响 submit 返回。

    反馈字段：
    - ``verdict_score`` (0/1)
    - ``verdict`` (字符串)
    - ``hint_level``（可选）
    - ``judge_cycle``（可选）
    """
    if not is_feedback_enabled():
        return
    if not run_id:
        return

    score = 1 if (verdict or "").strip().upper() == "AC" else 0
    comment_parts = []
    if session_id is not None:
        comment_parts.append(f"session_id={session_id}")
    comment = "; ".join(comment_parts) if comment_parts else None

    try:
        client = get_langsmith_client()
        if client is None:
            return
        # 主反馈：分数 + 原始 verdict
        client.create_feedback(
            run_id,
            key="verdict_score",
            score=float(score),
            value=verdict,
            comment=comment,
        )
        # 附加维度（若存在）
        if hint_level is not None:
            client.create_feedback(
                run_id, key="hint_level", value=int(hint_level), comment=comment
            )
        if judge_cycle is not None:
            client.create_feedback(
                run_id, key="judge_cycle", value=int(judge_cycle), comment=comment
            )
    except Exception as exc:  # 网络 / 鉴权失败，绝不影响主流程
        logger.warning("[observability] 回传 verdict feedback 失败（已忽略）: %s", exc)
