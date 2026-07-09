"""FastAPI 入口：``uv run code-tutor-api`` 的目标函数。"""

import uvicorn
from dotenv import load_dotenv

load_dotenv()


def main():
    uvicorn.run(
        "code_tutor_agent.api.main:app",
        host="0.0.0.0",
        port=8765,
        reload=True,
        reload_dirs=["src/code_tutor_agent"],
    )


if __name__ == "__main__":
    main()