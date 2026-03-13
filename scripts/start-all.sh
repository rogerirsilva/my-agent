#!/usr/bin/env bash
# Inicia o stack completo: OpenHands + Controller API + Bot Telegram
# Execute da raiz do projeto: ./scripts/start-all.sh
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "=== my-agent startup ==="

# 1. OpenHands (Docker)
OH_DIR="$ROOT/vendor/openhands"
if [ ! -f "$OH_DIR/.env" ]; then
  cp "$OH_DIR/.env.example" "$OH_DIR/.env"
  echo "[OpenHands] ATENÇÃO: edite $OH_DIR/.env com sua OPENHANDS_LLM_API_KEY"
  read -p "Pressione Enter após editar..."
fi
echo "[OpenHands] Iniciando container..."
(cd "$OH_DIR" && docker compose up -d)

# 2. Controller API
CTRL_DIR="$ROOT/apps/controller-api"
if [ ! -f "$CTRL_DIR/.env" ]; then
  cp "$CTRL_DIR/.env.example" "$CTRL_DIR/.env"
  echo "[Controller] ATENÇÃO: edite $CTRL_DIR/.env (CONTROLLER_API_KEY + TELEGRAM_BOT_TOKEN)"
  read -p "Pressione Enter após editar..."
fi
echo "[Controller] Iniciando FastAPI em background..."
cd "$CTRL_DIR"
uvicorn main:app --host 127.0.0.1 --port 8000 &
CTRL_PID=$!
echo "[Controller] PID $CTRL_PID — http://127.0.0.1:8000/docs"

# 3. Bot Telegram
BOT_DIR="$ROOT/apps/gateway-node"
if [ ! -f "$BOT_DIR/.env" ]; then
  cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
  echo "[Bot] ATENÇÃO: edite $BOT_DIR/.env com TELEGRAM_BOT_TOKEN"
  read -p "Pressione Enter após editar..."
fi
echo "[Bot] Iniciando gateway Telegram..."
cd "$BOT_DIR"
npm start
