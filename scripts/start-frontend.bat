@echo off
REM ──────────────────────────────────────────────────────
REM  Code Tutor Agent — 启动前端 Vite Dev Server
REM  监听: http://localhost:5173
REM  代理: /session , /health → 后端 localhost:8765
REM ──────────────────────────────────────────────────────

set PROJECT_DIR=%~dp0..
set FRONTEND_DIR=%PROJECT_DIR%\frontend

cd /d "%FRONTEND_DIR%"

if not exist "node_modules" (
    echo [INFO] node_modules 不存在，正在安装前端依赖...
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install 失败
        pause
        exit /b 1
    )
)

echo ╔══════════════════════════════════════╗
echo ║  Code Tutor Agent — 启动前端        ║
echo ║  http://localhost:5173               ║
echo ║  代理后端 → http://localhost:8765    ║
echo ╚══════════════════════════════════════╝
echo.

npm run dev

if %ERRORLEVEL% neq 0 (
    echo [ERROR] 前端启动失败
    pause
)