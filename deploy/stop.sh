#!/usr/bin/env bash
# Stop the engine (and optionally the tunnel) using pidfiles.
#
# Deliberately does NOT use `pkill -f`: a pattern like "cloudflared" or
# "uvicorn" also matches the SSH command line you are typing it from, so
# pkill -f kills your own session. Learned the hard way. pidfiles, or
# pgrep -x against the executable name, only.
set -uo pipefail
cd "$(dirname "$0")/.."

stop_one() {
    local name="$1" pidfile="$2"
    if [ ! -f "$pidfile" ]; then
        echo "$name: no pidfile, not running"
        return
    fi
    local pid
    pid="$(cat "$pidfile")"
    if ! ps -p "$pid" >/dev/null 2>&1; then
        echo "$name: pid $pid already gone"
        rm -f "$pidfile"
        return
    fi
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 25); do
        ps -p "$pid" >/dev/null 2>&1 || break
        sleep 0.2
    done
    if ps -p "$pid" >/dev/null 2>&1; then
        echo "$name: pid $pid did not exit, sending KILL"
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$pidfile"
    echo "$name: stopped (was pid $pid)"
}

stop_one uvicorn uvicorn.pid

if [ "${1:-}" = "--all" ]; then
    stop_one cloudflared cloudflared.pid
else
    echo "(tunnel left running; pass --all to stop it too)"
fi
