# ──────────────────────────────────────────────────────────
#  Code Tutor Agent — Makefile
#  Usage:
#    make server      启动后端 FastAPI (hot-reload)
#    make frontend    启动前端 Vite dev server
#    make all         同时启动前后端 (前端前台，后端后台)
#    make test        运行 pytest
#    make db          初始化数据库
#    make clean       清理缓存文件
#    make install     安装依赖 (backend + frontend)
#    help             显示本帮助
# ──────────────────────────────────────────────────────────

.PHONY: server frontend all server-bg test test-failed db clean install docker docker-prod docker-down help eval eval-outcome eval-trace eval-judge eval-judge-full eval-gate eval-regression eval-baseline-rebuild eval-test

# ── 默认目标 ──
help:
	@echo "Code Tutor Agent — 启动命令"
	@echo "————————————"
	@echo "  make server      启动后端 API (localhost:8765)"
	@echo "  make frontend    启动前端 dev (localhost:5173)"
	@echo "  make all         同时启动前后端"
	@echo "  make test        运行 pytest"
	@echo "  make db          初始化数据库"
	@echo "  make install     安装全部依赖"
	@echo "  make eval        Agent 回复质量评估 (零侵入·只读) → data/eval_report_*.json"
	@echo "  make eval-outcome  只跑 outcome 轨 (结果正确率, 最快)"
	@echo "  make eval-trace    只跑下轨 (链路分析准确率)"
	@echo "  make eval-judge  judge 自校准 (P/R/F1 + 门禁判定, 需 LLM 网关)"
	@echo "  make eval-gate   质量门禁判定 (致命幻觉>0 失败)"
	@echo "  make eval-regression  规则层基线回归 (防检测器被改坏)"
	@echo "  make eval-test   评估框架自身测试 (起真 PG, 168 项)"
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

# ── 测试 ──
test:
	uv run pytest -v --tb=short -k "not test_full_session_flow"

test-failed:
	uv run pytest -v --tb=long --lf -k "not test_full_session_flow"

# ── 数据库 ──
db:
	uv run python -c "from code_tutor_agent.db.database import init_db; init_db(); print('DB ready ✓')"

# ── Agent 回复质量评估（零侵入·只读；docs/eval-framework-design.md）──
# 连接串来源：EVAL_DATABASE_URL 优先，回退 DATABASE_URL（.env）
eval:
	uv run python scripts/eval_reply_quality.py --limit 50

# 只跑 outcome 轨（结果正确率，纯 SQL，最快）
eval-outcome:
	uv run python scripts/eval_reply_quality.py --mode outcome --limit 200

# 只跑下轨（链路分析准确率）
eval-trace:
	uv run python scripts/eval_reply_quality.py --mode trace --limit 100

# judge 自校准（docs §5.2）—— 需 LLM 网关；40 条金标约 3-5 分钟
eval-judge:
	uv run python scripts/eval_judge_calibration.py --gold data/gold/eval_gold_v2.json

# judge 自校准 + 双模型 Kappa + 位置偏置（更久）
eval-judge-full:
	uv run python scripts/eval_judge_calibration.py --gold data/gold/eval_gold_v2.json \
		--with-kappa --with-position

# 质量门禁判定（docs §8.1）—— 致命幻觉 > 0 → 失败；相对基线恶化 → 告警
eval-gate:
	uv run python scripts/eval_gate.py --report data/eval_report_*.json

# 规则层基线回归（防「检测器被改坏」；零 PG、零 LLM、零网络）
eval-regression:
	uv run python scripts/eval_regression.py

# 重建基线样本（改了检测器后跑，人工复核 expected_types）
eval-baseline-rebuild:
	uv run python scripts/build_regression_baseline.py

# 评估框架自身测试（pgserver 起真 PostgreSQL 16，无需 docker）
eval-test:
	uv run pytest tests/test_eval_outcome.py tests/test_reply_quality.py \
		tests/test_eval_e2e.py tests/test_eval_cli.py \
		tests/test_eval_trace.py tests/test_eval_trace_e2e.py \
		tests/test_eval_judge.py tests/test_eval_struct.py \
		tests/test_eval_gate.py -v --tb=short

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