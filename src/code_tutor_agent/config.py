"""LLM configuration: purpose → model registry.

Reads from .env:
- SENSENOVA_API_KEY, SENSENOVA_BASE_URL
- SENSENOVA_MODEL, SENSENOVA_MODEL1
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
    "sensenova-deepseek": {
        "model": os.getenv("SENSENOVA_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "sensenova": {
        "model": os.getenv("SENSENOVA_MODEL1"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
}

# ── 业务用途 → 模型配置映射 ──
# 业务代码只表达"用途"，不感知具体模型。
# 改模型只需改这里，业务代码一行不动。
PURPOSE_CONFIGS = {
    # === 节点 ===
    "chat":                 {"alias": "sensenova-deepseek", "temperature": 0.7, "streaming": True},
    "tutor-eval":           {"alias": "sensenova-deepseek", "temperature": 0.1},
    "tutor-generate":       {"alias": "sensenova-deepseek", "temperature": 0.4},
    "tutor-router":         {"alias": "sensenova-deepseek", "temperature": 0.2},
    "generator":            {"alias": "sensenova-deepseek", "temperature": 0.3},

    # === Agent 模块 ===
    "dialog":               {"alias": "sensenova-deepseek", "temperature": 0.3, "max_tokens": 512},
    "dialog-stream":        {"alias": "sensenova-deepseek", "temperature": 0.7, "streaming": True},
    "judge":                {"alias": "sensenova-deepseek", "temperature": 0.7},
    "problem":              {"alias": "sensenova-deepseek", "temperature": 0.7, "max_tokens": 8192},

    # === 上下文管理 ===
    "context-summary":      {"alias": "sensenova-deepseek", "temperature": 0.3},

    # === API 路由 ===
    "api-generation":       {"alias": "sensenova-deepseek", "temperature": 0.3},
    "api-generation-high":  {"alias": "sensenova-deepseek", "temperature": 0.5},
    "api-chat":             {"alias": "sensenova-deepseek", "temperature": 0.7, "streaming": True},
    "api-chat-query":       {"alias": "sensenova-deepseek", "temperature": 0.7},

    # === 沙箱 ===
    "adversarial-eval":     {"alias": "sensenova-deepseek", "temperature": 0.3},
    "adversarial-eval-low": {"alias": "sensenova-deepseek", "temperature": 0.2},

    # === Skill 引擎 ===
    "skill-engine":         {"alias": "sensenova-deepseek", "temperature": 0.7},

    # === 基准测试 ===
    "benchmark":            {"alias": "sensenova", "temperature": 0.5},
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


# ── skill-engine CLI 逃生舱 ──
def get_skill_engine_dir() -> str:
    """skill-engine 项目根目录（含 skills/ 子目录）。

    从环境变量 SKILL_ENGINE_DIR 读取，默认 D:/Code/PycharmProjects/skill-engine。
    run 命令用 Path.cwd()/skills 扫描 skill，因此 spawn 时必须 cwd 指到这里。
    """
    return os.getenv(
        "SKILL_ENGINE_DIR",
        "D:/Code/PycharmProjects/skill-engine",
    )


def get_skill_engine_cli_timeout() -> int:
    """CLI 子进程超时（秒），默认 60。"""
    return int(os.getenv("SKILL_ENGINE_CLI_TIMEOUT", "60"))


# ── skill-engine import 主通道（engine_adapter）──
def get_skill_engine_purpose() -> str:
    """adapter 通道使用的 LLM 用途（单一真源），默认 'skill-engine'。

    三通道（adapter import / cli_runner --purpose / CI --purpose）都解析到同一用途，
    杜绝 "CLI 读另一套 env" 的分裂。
    """
    return os.getenv("SKILL_ENGINE_PURPOSE", "skill-engine")


def get_skill_engine_skills_root() -> str:
    """本系统内置 skill defs 目录（随仓发布，DP-3）；adapter 与 cli 共用。

    默认指向 src/code_tutor_agent/skills/defs（Phase 0 已把两个真实 def
    还原到此处）。discover(roots=[此绝对路径]) 直接扫描，不经 cwd。
    """
    return os.getenv(
        "SKILL_ENGINE_SKILLS_ROOT",
        os.path.join(os.path.dirname(__file__), "skills", "defs"),
    )


#: 允许通过 CLI 执行的 skill 名白名单（防止任意 skill 名注进 subprocess）
#: 出题已收口到 ProblemAgent（原生 LLM + 静态兜底），不再经 skill-engine 出题，
#: 故此处仅保留「详细题解」这类仍走 skill-engine 的能力。
SKILL_ENGINE_CLI_ALLOWLIST: frozenset[str] = frozenset(
    os.getenv(
        "SKILL_ENGINE_CLI_ALLOWLIST",
        "cta-generate-solution",
    ).split(",")
)