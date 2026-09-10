#!/usr/bin/env bash
# Start the KERS engine, detached, with a pidfile.
#
# Binds 127.0.0.1 deliberately. The service is fronted by a tunnel (see
# tunnel.sh); binding 0.0.0.0 would expose the webhook endpoint directly on
# the host's network interface with no signature check in front of it.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "ERROR: .env not found. Copy deploy/secrets.env.example to .env and fill it in." >&2; exit 1; }
[ -x .venv/bin/uvicorn ] || { echo "ERROR: .venv missing. See deploy/DEPLOY.md." >&2; exit 1; }

if [ -f uvicorn.pid ] && ps -p "$(cat uvicorn.pid)" >/dev/null 2>&1; then
    echo "already running as pid $(cat uvicorn.pid)"
    exit 0
fi

set -a; . ./.env; set +a
: "${OPENAI_API_KEY:?OPENAI_API_KEY is empty in .env}"
: "${ZENDESK_API_TOKEN:?ZENDESK_API_TOKEN is empty in .env}"

mkdir -p logs data ticket_files

nohup .venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 \
    --log-config uvicorn_log_config.json \
    > logs/uvicorn.out 2>&1 < /dev/null &
echo $! > uvicorn.pid
disown 2>/dev/null || true

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":8000 " && break
    sleep 0.2
done
sleep 1

if ! ps -p "$(cat uvicorn.pid)" >/dev/null 2>&1; then
    echo "FAILED to start. Last 20 lines of logs/uvicorn.out:" >&2
    tail -20 logs/uvicorn.out >&2
    rm -f uvicorn.pid
    exit 1
fi

echo "started pid $(cat uvicorn.pid)"
echo "health: $(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)"
echo "prompt: $(.venv/bin/python -c 'from app.config import PROMPT_VERSION; print(PROMPT_VERSION)')"
