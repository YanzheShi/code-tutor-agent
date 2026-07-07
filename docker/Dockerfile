# ── Build stage ──
FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS builder

WORKDIR /app

# 先安装依赖（利用 Docker 层缓存）
COPY pyproject.toml .
RUN uv sync --frozen --no-dev

# ── Runtime stage ──
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制源码
COPY src/ src/
COPY data/ data/
COPY .env.template .env.example

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')" || exit 1

CMD ["uvicorn", "src.code_tutor_agent.api.main:app", \
     "--host", "0.0.0.0", "--port", "8765"]