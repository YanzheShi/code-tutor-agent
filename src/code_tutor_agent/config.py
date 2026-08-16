"""LLM configuration: purpose → model registry.

Reads from .env:
- LLM_MODEL, LLM_BASE_URL, LLM_API_KEY
- LLM_MODEL_ALT, LLM_BASE_URL_ALT, LLM_API_KEY_ALT
- etc.

业务代码只通过 get_llm(purpose="xxx") 获取模型实例，不关心具体用哪个模型。
模型选择由下方的 PURPOSE_CONFIGS 统一控制，改模型只需改这一个文件。
"""

import os
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# 在模块加载时加载 .env，使 LLM_CONFIGS 能读取环境变量
load_dotenv()

# ── 模型注册表（alias → provider 配置） ──
# 这里只定义"有哪些模型可用"，不决定业务用哪个。
# PURPOSE_CONFIGS 引用这里的 alias。
LLM_CONFIGS = {
    "default": {
        "model": os.getenv("LLM_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("LLM_BASE_URL"),
        "api_key": os.getenv("LLM_API_KEY"),
    },
    "secondary": {
        "model": os.getenv("LLM_MODEL_ALT"),
        "model_provider": "openai",
        "base_url": os.getenv("LLM_BASE_URL_ALT"),
        "api_key": os.getenv("LLM_API_KEY_ALT"),
    },
}

# ── 业务用途 → 模型配置映射 ──
# 业务代码只表达"用途"，不感知具体模型。
# 改模型只需改这里，业务代码一行不动。
PURPOSE_CONFIGS = {
    # === 节点 ===
    "chat":                 {"alias": "default", "temperature": 0.7, "streaming": True},
    "tutor-eval":           {"alias": "default", "temperature": 0.1},
    "tutor-generate":       {"alias": "default", "temperature": 0.4},
    "tutor-router":         {"alias": "default", "temperature": 0.2},
    "generator":            {"alias": "default", "temperature": 0.3},

    # === Agent 模块 ===
    "dialog":               {"alias": "default", "temperature": 0.3},
    "dialog-stream":        {"alias": "default", "temperature": 0.7, "streaming": True},
    "judge":                {"alias": "default", "temperature": 0.7, "max_tokens": 4096},
    # 这里
    "problem":              {"alias": "default", "temperature": 0.7, "max_tokens": 16384},

    # === 上下文管理 ===
    "context-summary":      {"alias": "default", "temperature": 0.3},

    # === Agent memory(语义抽取式用户记忆,见 docs/agent-memory-design.md)===
    "memory-extract":       {"alias": "default", "temperature": 0.1},

    # === API 路由 ===
    "api-generation":       {"alias": "default", "temperature": 0.3},
    "api-generation-high":  {"alias": "secondary", "temperature": 0.5, "max_tokens": 16384},
    "api-chat":             {"alias": "default", "temperature": 0.7, "streaming": True},
    "api-chat-query":       {"alias": "default", "temperature": 0.7},

    # === 沙箱 ===
    "adversarial-eval":     {"alias": "default", "temperature": 0.3},
    "adversarial-eval-low": {"alias": "default", "temperature": 0.2},

    # === 编辑轨迹分析（错误模式画像 feeder，见 docs/error-mode-tracking-design.md）===
    "edit-trace":           {"alias": "default", "temperature": 0.1},

    # === 基准测试 ===
    "benchmark":            {"alias": "secondary", "temperature": 0.5},
}


def get_llm(purpose: str, **kwargs):
    """根据业务用途获取大模型实例。

    业务代码只表达"用途"（如 ``purpose="tutor-eval"``），
    具体用哪个模型由 ``PURPOSE_CONFIGS`` 统一控制。

    Args:
        purpose: 业务用途，对应 PURPOSE_CONFIGS 中的 key。
        **kwargs: 额外的模型参数，会覆盖用途配置中的默认值。

    Returns:
        LangChain chat model 实例。

    Raises:
        ValueError: 用途名不存在，或模型配置不完整。
    """
    if purpose not in PURPOSE_CONFIGS:
        raise ValueError(
            f"未知的用途: '{purpose}'，可选: {list(PURPOSE_CONFIGS.keys())}"
        )

    purpose_cfg = PURPOSE_CONFIGS[purpose].copy()
    alias = purpose_cfg.pop("alias")

    if alias not in LLM_CONFIGS:
        raise ValueError(
            f"用途 '{purpose}' 引用了未知的模型别名: '{alias}'，"
            f"可选: {list(LLM_CONFIGS.keys())}"
        )

    # 合并：模型注册表配置 + 用途默认参数 + 调用方覆盖参数
    config = LLM_CONFIGS[alias].copy()
    config.update(purpose_cfg)   # 用途默认参数（temperature, streaming 等）
    config.update(kwargs)        # 调用方显式覆盖

    # 确保必填项不为空
    if not config.get("model") or not config.get("api_key"):
        raise ValueError(
            f"模型 '{alias}'（用途 '{purpose}'）的配置不完整，"
            f"请检查 .env 文件中的环境变量"
        )

    return init_chat_model(**config)


# ── Checkpoint DB (LangGraph session persistence) ──
def get_checkpoint_db_path() -> str:
    """获取 LangGraph checkpointer 的 SQLite 数据库路径。

    从环境变量 CHECKPOINT_DB_PATH 读取，默认 data/checkpoints.db。
    """
    default_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data",
        "checkpoints.db",
    )
    return os.getenv("CHECKPOINT_DB_PATH", default_path)


# ── Session TTL (auto-cleanup) ──
def get_session_ttl_hours() -> int:
    """会话过期时间（小时），超过后自动清理。

    从环境变量 SESSION_TTL_HOURS 读取，默认 168（7 天）。
    """
    return int(os.getenv("SESSION_TTL_HOURS", "168"))


def get_cleanup_interval_minutes() -> int:
    """自动清理任务运行间隔（分钟）。

    从环境变量 SESSION_CLEANUP_INTERVAL_MINUTES 读取，默认 60（1 小时）。
    """
    return int(os.getenv("SESSION_CLEANUP_INTERVAL_MINUTES", "60"))