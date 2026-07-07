#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
#  Code Tutor Agent — 一键启动 (Unix/git-bash)
#  后端: http://localhost:8765
#  前端: http://localhost:5173
#
#  用法:
#    bash scripts/start-all.sh
# ──────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")/.."

# ── PATH 兜底：确保 uv / npm 能找到 ──
export PATH="$PATH:/d/app/Hermes/bin:/c/Users/Andre/AppData/Local/hermes/bin"
# make (GnuWin32)
export PATH="$PATH:/c/Program Files (x86)/GnuWin32/bin"
# uv 在 Windows 上还可能装在 WinGet 路径下
export PATH="$PATH:/c/Users/Andre/AppData/Local/Microsoft/WinGet/Packages/astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe"

# ── 前置检查 ──
command -v uv >/dev/null 2>&1 || {
  echo "[ERROR] 找不到 uv 命令。请确保 uv 已安装或在 PATH 中"
  echo "        安装: curl -fsSL https://astral.sh/uv/install.sh | bash"
  exit 1
}
command -v curl >/dev/null 2>&1 || { echo "[WARN] 未找到 curl，将跳过后端健康检查"; CURL_AVAILABLE=0; }

echo "╔══════════════════════════════════════╗"
echo "║  Code Tutor Agent — 一键启动         ║"
echo "║  后端: http://localhost:8765          ║"
echo "║  前端: http://localhost:5173          ║"
echo "║  API: http://localhost:8765/docs      ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 检查 .env ──
if [ ! -f .env ] && [ -f .env.template ]; then
  echo "[WARN] .env 不存在！正在从 .env.template 复制..."
  cp .env.template .env
  echo "[WARN] 请编辑 .env 填入 API key 后再启动"
fi

# ── 检查前端依赖 ──
if [ ! -d frontend/node_modules ]; then
  echo "[INFO] 安装前端依赖..."
  (cd frontend && npm install)
fi

cleanup() {
  echo ""
  echo "[INFO] 正在关闭..."
  kill $BACKEND_PID 2>/dev/null || true
  kill $FRONTEND_PID 2>/dev/null || true
  wait $BACKEND_PID 2>/dev/null || true
  wait $FRONTEND_PID 2>/dev/null || true
  echo "[INFO] 已关闭"
}
trap cleanup EXIT INT TERM

# ── 启动后端 ──
echo "[1/2] 启动后端..."
uv run uvicorn src.code_tutor_agent.api.main:app \
  --host 0.0.0.0 --port 8765 \
  --reload --reload-dir src/code_tutor_agent \
  --log-level info \
  > logs/backend.log 2>&1 &
BACKEND_PID=$!
echo "[OK] 后端 PID=$BACKEND_PID  → tail -f logs/backend.log"

# ── 等待后端就绪 ──
echo "[WAIT] 等待后端就绪..."
if [ "${CURL_AVAILABLE:-1}" -eq 1 ]; then
  for i in $(seq 1 15); do
    if curl -sf http://localhost:8765/health > /dev/null 2>&1; then
      echo "[OK] 后端就绪 ✓"
      break
    fi
    sleep 2
  done
else
  echo "[WARN] 跳过健康检查（未安装 curl），等待 5 秒后继续..."
  sleep 5
fi

# ── 启动前端 ──
echo "[2/2] 启动前端..."
(cd frontend && npm run dev > ../logs/frontend.log 2>&1) &
FRONTEND_PID=$!
echo "[OK] 前端 PID=$FRONTEND_PID → tail -f logs/frontend.log"

echo "[OK] 全部就绪！日志:"
echo "  后端 → tail -f logs/backend.log"
echo "  前端 → tail -f logs/frontend.log"
echo "  http://localhost:5173  |  http://localhost:8765/docs"
echo "[INFO] Ctrl+C 关闭所有服务"

# 保持前台进程运行
wait