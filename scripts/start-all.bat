@echo off
REM ──────────────────────────────────────────────────────
REM  Code Tutor Agent — 一键启动 (前后端同时)
REM  后端: http://localhost:8765  (一个新终端窗口)
REM  前端: http://localhost:5173  (当前窗口)
REM
REM  确保:
REM    1. uv sync          — 安装后端 Python 依赖
REM    2. .env 配置正确    — 模型 API key
REM    3. npm install      — 前端依赖 (脚本会自动处理)
REM ──────────────────────────────────────────────────────

set PROJECT_DIR=%~dp0..
cd /d "%PROJECT_DIR%"

echo ╔══════════════════════════════════════╗
echo ║  Code Tutor Agent — 一键启动         ║
echo ║  后端: http://localhost:8765          ║
echo ║  前端: http://localhost:5173          ║
echo ║  API: http://localhost:8765/docs      ║
echo ╚══════════════════════════════════════╝
echo.

REM ── 检查 .env ──
if not exist ".env" (
    if exist ".env.template" (
        echo [WARN] .env 文件不存在！正在从 .env.template 复制...
        copy .env.template .env
        echo [WARN] 请编辑 .env 填入 API key 后再启动
        echo [WARN] 按任意键打开 .env 编辑，或 Ctrl+C 退出
        pause
    )
)

REM ── 启动后端 (新窗口, 后台) ──
echo [1/2] 启动后端...
start "CodeTutor-Backend" /D "%PROJECT_DIR%" cmd /c "scripts\start-server.bat"

REM ── 等待后端就绪 ──
echo [WAIT] 等待后端就绪...
:wait_loop
timeout /t 2 /nobreak >nul
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:8765/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    set /a attempts+=1
    if !attempts! geq 15 (
        echo [WARN] 后端未在 30s 内就绪，继续启动前端...
        goto start_frontend
    )
    goto wait_loop
)
echo [OK] 后端就绪 ✓

:start_frontend
REM ── 启动前端 (当前窗口, 前台) ──
echo [2/2] 启动前端...
call scripts\start-frontend.bat

echo.
echo [INFO] 前后端已关闭。