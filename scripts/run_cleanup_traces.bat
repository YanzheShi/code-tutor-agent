@echo off
REM ---------------------------------------------------------------------------
REM Purge stale code-tutor trace data (edit_traces and 4 derived tables).
REM Invoked by Windows Task Scheduler; see docs for the schtasks command.
REM
REM NOTE: this file is intentionally ASCII-only. Windows batch files are read
REM using the ANSI codepage, so non-ASCII characters (e.g. Chinese comments)
REM may be mangled and break the script.
REM
REM Retention is controlled by --days (default 30). Change it here if you want
REM the scheduled run to keep a different window.
REM ---------------------------------------------------------------------------
setlocal

set "ROOT=D:\Code\PycharmProjects\code-tutor-agent"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "LOG=%ROOT%\logs\cleanup_traces.log"

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

if not exist "%PY%" (
  echo [%date% %time%] python not found: %PY% 1>>"%LOG%" 2>&1
  exit /b 2
)

cd /d "%ROOT%"
"%PY%" "%ROOT%\scripts\cleanup_traces.py" --days 30 --json 1>>"%LOG%" 2>&1

exit /b %ERRORLEVEL%
