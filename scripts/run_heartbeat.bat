@echo off
rem Heartbeat check for Code Tutor Agent (scheduled task entry).
rem All output to heartbeat.log in data dir. ASCII only.

set PYTHON=D:\Dev\Python312\python.exe
if exist "%PYTHON%" goto run
set PYTHON=python

:run
cd /d D:\Code\PycharmProjects\code-tutor-agent
"%PYTHON%" scripts\heartbeat_check.py >> data\heartbeat.log 2>&1
exit /b %ERRORLEVEL%
