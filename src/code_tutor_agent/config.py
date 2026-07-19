"""LLM configuration: model alias -> provider, temperature, base URL.

Reads from .env:
- AGNES_API_KEY, AGNES_BASE_URL
- LLM_MODEL_AGNES, LLM_MODEL_AGNES_STREAM
- OPENAI_API_KEY etc.
"""

import os
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# 在模块加载时加载 .env，使 LLM_CONFIGS 能读取环境变量
load_dotenv()

# 模型别名 -> 具体配置的映射表
# 你可以在这里随时增加或修改你的模型
LLM_CONFIGS = {
    "sensenova": {
        "model": os.getenv("SENSENOVA_MODEL1"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "sensenova-deepseek": {
        "model": os.getenv("SENSENOVA_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "gpt-4o": {
        "model": "gpt-4o-mini",
        "model_provider": "openai",
        "api_key": os.getenv("OPENAI_API_KEY"),
    },
    "deepseek": {
        # 假设你装了 langchain-deepseek
        "model": "deepseek-coder",
        "model_provider": "deepseek",
        "api_key": os.getenv("DEEPSEEK_API_KEY"),
    },
    "qwen": {
        # 通义千问兼容 openai 接口
        "model": "qwen-plus",
        "model_provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": os.getenv("DASHSCOPE_API_KEY"),
    },
    "agnes": {
        "model": os.getenv("AGNES_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("AGNES_BASE_URL"),
        "api_key": os.getenv("AGNES_API_KEY"),
    },
    "agnes-stream": {
        "model": os.getenv("AGNES_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("AGNES_BASE_URL"),
        "api_key": os.getenv("AGNES_API_KEY"),
        "streaming": True
    }
}


def get_llm(alias: str = "sensenova", **kwargs):
    """
    根据别名获取大模型实例

    参数:
        alias: 模型别名，如 "sensenova", "gpt-4o"
        **kwargs: 额外的模型参数，如 temperature=0.7, streaming=True
    """
    if alias not in LLM_CONFIGS:
        raise ValueError(f"未找到模型别名: '{alias}'，可选: {list(LLM_CONFIGS.keys())}")

    # 复制配置，避免修改原字典
    config = LLM_CONFIGS[alias].copy()

    # 允许覆盖参数（如 temperature, streaming 等）
    config.update(kwargs)

    # 确保必填项不为空
    if not config.get("model") or not config.get("api_key"):
        raise ValueError(f"模型 '{alias}' 的配置不完整，请检查 .env 文件中的环境变量")

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
def get_skill_engine_llm_alias() -> str:
    """adapter 通道使用的 LLM 别名（单一真源），默认 'agnes'。

    三通道（adapter import / cli_runner --llm / CI --llm）都解析到同一别名，
    杜绝 "CLI 读另一套 env" 的分裂。
    """
    return os.getenv("SKILL_ENGINE_LLM_ALIAS", "agnes")


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
#: DP-4：精简为两个真实 def；移除幽灵名 cta-generate-detailed-solution（无对应 def）。
SKILL_ENGINE_CLI_ALLOWLIST: frozenset[str] = frozenset(
    os.getenv(
        "SKILL_ENGINE_CLI_ALLOWLIST",
        "cta-generate-problem,cta-generate-solution",
    ).split(",")
)
