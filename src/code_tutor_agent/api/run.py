"""FastAPI entry point — ``uv run code-tutor-api`` target."""

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