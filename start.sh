#!/usr/bin/env bash
# BookMind / 学迹 — single-machine startup (ARCHITECTURE §13 fallback path).
# No Docker / PostgreSQL needed. Starts the API (serving the frontend at /ui)
# in offline deterministic mode. Set USTC_LLM_API_KEY for live-model enhancement.
#
# Usage:  bash start.sh          # then open http://localhost:18765/ui

set -e
cd "$(dirname "$0")"

export PYTHONPATH=backend
export PYTHONUTF8=1

PROJECT_PYTHON=".venv/bin/python"
if [ ! -x "$PROJECT_PYTHON" ]; then
  echo "学迹：尚未创建项目运行环境。"
  echo "请先执行：python3 -m venv .venv"
  echo "          .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi
if ! "$PROJECT_PYTHON" -c "import uvicorn, bookmind" >/dev/null 2>&1; then
  echo "学迹：项目依赖不完整，请执行："
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

# Host/port default to 127.0.0.1:18765; override via BOOKMIND_HOST / BOOKMIND_PORT.
HOST="${BOOKMIND_HOST:-127.0.0.1}"
PORT="${BOOKMIND_PORT:-18765}"

echo "学迹 / BookMind — 单机启动（SQLite / 内存回退路径）"
echo "前端:  http://${HOST}:${PORT}/ui"
echo "API:   http://${HOST}:${PORT}/docs"
if [ -n "$USTC_LLM_API_KEY" ] || { [ -f .env ] && grep -Eq '^USTC_LLM_API_KEY=.+$' .env; }; then
  echo "模型:  已设置 USTC_LLM_API_KEY（实时模型增强）"
else
  echo "模型:  离线确定性模式（未设 USTC_LLM_API_KEY）"
fi
echo "Ctrl+C 退出。"
echo ""

"$PROJECT_PYTHON" -m uvicorn bookmind.api.app:app --host "$HOST" --port "$PORT"
