#!/usr/bin/env bash
# Start a Cloudflare quick tunnel in front of the engine and print its URL.
#
# --no-autoupdate is not optional. Without it, cloudflared checks for a new
# version once every 24h and, on finding one, replaces its binary and EXITS,
# expecting an init system to restart it. With plain nohup there is nothing
# to restart it, so the tunnel silently dies. That happened once and cost a
# full day of dropped webhooks before anyone noticed.
#
# Quick tunnels get a random hostname that CHANGES on every restart, so the
# helpdesk webhook must be repointed each time this runs. For anything
# long-lived, use a named tunnel with a stable hostname instead.
set -euo pipefail
cd "$(dirname "$0")/.."

CF="${CLOUDFLARED:-$HOME/bin/cloudflared}"
[ -x "$CF" ] || { echo "ERROR: cloudflared not found at $CF (see deploy/DEPLOY.md)" >&2; exit 1; }

if [ -f cloudflared.pid ] && ps -p "$(cat cloudflared.pid)" >/dev/null 2>&1; then
    echo "already running as pid $(cat cloudflared.pid)"
    grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/cloudflared.out | head -1
    exit 0
fi

mkdir -p logs
rm -f logs/cloudflared.out

nohup "$CF" tunnel --no-autoupdate --url http://localhost:8000 \
    > logs/cloudflared.out 2>&1 < /dev/null &
echo $! > cloudflared.pid
disown 2>/dev/null || true

URL=""
for _ in $(seq 1 40); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/cloudflared.out 2>/dev/null | head -1)"
    [ -n "$URL" ] && break
    sleep 0.5
done

if [ -z "$URL" ]; then
    echo "FAILED to get a tunnel URL. Log:" >&2
    tail -30 logs/cloudflared.out >&2
    exit 1
fi

echo "pid $(cat cloudflared.pid)"
echo
echo "  webhook endpoint:"
echo "    ${URL}/webhooks/zendesk/events"
echo
echo "  point the helpdesk at that URL. it changes every time this restarts."
