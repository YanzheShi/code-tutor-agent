@echo off
REM ──────────────────────────────────────────────────────
REM  Code Tutor Agent — 启动后端 FastAPI
REM  监听: http://localhost:8765
REM  热重载: 监听 src/code_tutor_agent/ 下的文件变更
REM ──────────────────────────────────────────────────────

set PROJECT_DIR=%~dp0..
cd /d "%PROJECT_DIR%"

echo ╔══════════════════════════════════════╗
echo ║  Code Tutor Agent — 启动后端        ║
echo ║  http://localhost:8765               ║
echo ║  Docs: http://localhost:8765/docs    ║
echo ║  日志: logs\backend.log              ║
echo ╚══════════════════════════════════════╝
echo.

mkdir logs 2>nul

uv run uvicorn src.code_tutor_agent.api.main:app ^
    --host 0.0.0.0 ^
    --port 8765 ^
    --reload ^
    --reload-dir src\code_tutor_agent ^
    --log-level info ^
    > logs\backend.log 2>&1

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] 后端启动失败。确保：
    echo   1. .venv 已创建且依赖已安装 (uv sync)
    echo   2. .env 文件配置正确
    pause
)