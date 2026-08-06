# ── Build stage ──
FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS builder

WORKDIR /app

# 仅复制元数据，利用 Docker 层缓存；--no-install-project 避免在 builder 中安装项目本体
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── Runtime stage ──
FROM python:3.12-slim-bookworm

WORKDIR /app

# 复用 builder 阶段生成的 .venv
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"

# 复制源码（PYTHONPATH 使 code_tutor_agent 可导入）
COPY src/ src/
COPY data/ data/
COPY .env.example .env.example

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')" || exit 1

CMD ["uvicorn", "code_tutor_agent.api.main:app", "--host", "0.0.0.0", "--port", "8765"]