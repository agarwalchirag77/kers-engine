#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
source deploy/secrets.env
set +a
exec ./.venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --log-config uvicorn_log_config.json
