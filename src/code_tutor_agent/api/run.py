"""FastAPI 入口：``uv run code-tutor-api`` 的目标函数。

⚠️ 本入口仅用于本地开发（默认开热重载）。生产部署一律走 docker/Dockerfile 的
CMD（裸 uvicorn，无 reload）——开发服务器不得直接暴露公网（审计 F-07）。
"""

import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()


def _reload_enabled() -> bool:
    """热重载开关：CTA_API_RELOAD / UVICORN_RELOAD（compose 用的名字）任一可关。"""
    for name in ("CTA_API_RELOAD", "UVICORN_RELOAD"):
        raw = os.getenv(name, "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return False
        if raw in ("1", "true", "yes", "on"):
            return True
    return True  # 本地开发默认开


def main():
    uvicorn.run(
        "code_tutor_agent.api.main:app",
        host="0.0.0.0",
        port=8765,
        reload=_reload_enabled(),
        reload_dirs=["src/code_tutor_agent"],
    )


if __name__ == "__main__":
    main()