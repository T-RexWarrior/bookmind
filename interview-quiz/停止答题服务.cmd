@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist quiz_server.pid goto notrunning

set "PID="
set /p PID=<quiz_server.pid
if defined PID taskkill /PID %PID% /F >nul 2>nul
del /q quiz_server.pid >nul 2>nul
echo 已停止答题服务。
goto :eof

:notrunning
echo 没有找到 quiz_server.pid，答题服务可能未运行。
