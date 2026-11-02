@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo 未找到 Python，改用浏览器本地存储。
  start "" "%~dp0答题闯关.html"
  exit /b 0
)

start "BookMind Quiz Server" /min %PY% quiz_server.py --open
