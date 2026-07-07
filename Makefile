# ──────────────────────────────────────────────────────────
#  Code Tutor Agent — Makefile
#  Usage:
#    make server      启动后端 FastAPI (hot-reload)
#    make frontend    启动前端 Vite dev server
#    make all         同时启动前后端 (前端前台，后端后台)
#    make cli         运行 CLI 交互模式
#    make test        运行 pytest
#    make db          初始化数据库
#    make clean       清理缓存文件
#    make install     安装依赖 (backend + frontend)
#    help             显示本帮助
# ──────────────────────────────────────────────────────────

.PHONY: server frontend all cli test db clean install help

# ── 默认目标 ──
help:
	@echo "Code Tutor Agent — 启动命令"
	@echo "————————————"
	@echo "  make server      启动后端 API (localhost:8765)"
	@echo "  make frontend    启动前端 dev (localhost:5173)"
	@echo "  make all         同时启动前后端"
	@echo "  make cli         运行 CLI 交互模式"
	@echo "  make test        运行 pytest"
	@echo "  make db          初始化数据库"
	@echo "  make install     安装全部依赖"
	@echo "  make clean       清理 __pycache__ / .pytest_cache"

# ── 后端 ──
server:
	@mkdir -p logs
	uv run uvicorn src.code_tutor_agent.api.main:app \
		--host 0.0.0.0 \
		--port 8765 \
		--reload \
		--reload-dir src/code_tutor_agent \
		--log-level info | tee logs/backend.log

# 后台启动 + 日志落盘（tail -f logs/backend.log 查看）
server-bg:
	uv run uvicorn src.code_tutor_agent.api.main:app \
		--host 0.0.0.0 \
		--port 8765 \
		--reload \
		--reload-dir src/code_tutor_agent \
		--log-level info \
		> logs/backend.log 2>&1 &
	@echo "后端已后台启动 → tail -f logs/backend.log"

# ── 前端 ──
frontend:
	@echo "→ 启动 Vite dev server (localhost:5173)..."
	cd frontend && npm run dev

# ── 前后端同时启动 ──
all:
	@echo "╔══════════════════════════════════════╗"
	@echo "║  Code Tutor Agent — 一键启动         ║"
	@echo "║  后端: http://localhost:8765          ║"
	@echo "║  前端: http://localhost:5173          ║"
	@echo "╚══════════════════════════════════════╝"
	@echo ""
	$(MAKE) server &
	@sleep 3
	$(MAKE) frontend

# ── CLI ──
cli:
	uv run python src/code_tutor_agent/main.py

# ── 测试 ──
test:
	uv run pytest -v --tb=short -k "not test_full_session_flow"

test-failed:
	uv run pytest -v --tb=long --lf -k "not test_full_session_flow"

# ── 数据库 ──
db:
	uv run python -c "from code_tutor_agent.db.database import init_db; init_db(); print('DB ready ✓')"

# ── Docker ──
docker:
	docker compose -f docker/docker-compose.yml up -d

docker-prod:
	docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d

docker-down:
	docker compose -f docker/docker-compose.yml down

# ── 安装依赖 ──
install:
	uv sync
	cd frontend && npm install

# ── 清理 ──
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf .venv
	rm -rf frontend/node_modules
	@echo "cleaned ✓"