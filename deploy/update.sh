#!/usr/bin/env bash
# Pull the latest code and restart the engine, with a health gate and an
# automatic rollback if the new revision does not come up.
#
# Usage:
#   ./deploy/update.sh              pull, install, test, restart, verify
#   ./deploy/update.sh --dry-run    show what would be pulled, change nothing
#   ./deploy/update.sh --force      redeploy even if there are no new commits
#   ./deploy/update.sh --skip-tests skip pytest before restarting
#   ./deploy/update.sh --no-restart update the working tree only
#   ./deploy/update.sh --check-tunnel  report the tunnel URL and exit
#
# Deliberately does NOT touch kers-tunnel. Under a Cloudflare *quick* tunnel
# every cloudflared restart hands out a NEW random hostname, which silently
# breaks the helpdesk webhook — a code deploy must never do that as a side
# effect. Restart the tunnel by hand when you mean to, then re-register the
# URL (see deploy/DEPLOY.md).

set -euo pipefail

# --- run from a copy -------------------------------------------------------
# This script lives in the repo it is about to `git pull`, and bash reads a
# script incrementally as it executes. If the pull rewrites this file
# mid-run, bash carries on reading at the old byte offset and executes
# garbage. So: re-exec from a private copy, then update the real tree.
if [ "${KERS_UPDATE_REEXEC:-}" != "1" ]; then
    _self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    _tmp="$(mktemp /tmp/kers-update.XXXXXX)"
    cp "$_self" "$_tmp"
    chmod +x "$_tmp"
    KERS_UPDATE_REEXEC=1 KERS_UPDATE_TMP="$_tmp" KERS_UPDATE_ORIGIN="$_self" \
        exec "$_tmp" "$@"
fi
# Unlinking a running script is safe on Linux — bash holds it open by fd.
rm -f "${KERS_UPDATE_TMP:-}"
cd "$(dirname "${KERS_UPDATE_ORIGIN}")/.."
ROOT="$(pwd)"

DRY_RUN=0 FORCE=0 SKIP_TESTS=0 NO_RESTART=0 CHECK_TUNNEL=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN=1 ;;
        --force)        FORCE=1 ;;
        --skip-tests)   SKIP_TESTS=1 ;;
        --no-restart)   NO_RESTART=1 ;;
        --check-tunnel) CHECK_TUNNEL=1 ;;
        -h|--help)      awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' \
                          "$KERS_UPDATE_ORIGIN"; exit 0 ;;
        *)            echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
say "Preflight"

[ -d .git ]           || die "$ROOT is not a git checkout"
[ -f .env ]           || die ".env not found. See deploy/DEPLOY.md step 3."
[ -x .venv/bin/python ] || die ".venv missing. See deploy/DEPLOY.md step 2."

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || die "detached HEAD. Check out a branch first."

set -a; . ./.env; set +a
PORT="${ERS_PORT:-8080}"

if systemctl list-unit-files kers-engine.service --no-legend 2>/dev/null | grep -q .; then
    MODE=systemd
else
    MODE=scripts
fi
info "repo    $ROOT"
info "branch  $BRANCH"
info "port    $PORT"
info "mode    $MODE"

# --- tunnel URL -----------------------------------------------------------
# A Cloudflare *quick* tunnel gets a new random hostname every time
# cloudflared restarts — a reboot, a crash-restart, an auto-update. systemd
# brings the process back, the helpdesk stays pointed at the dead hostname,
# and nothing on this host looks wrong: /health is 200 and the engine is
# happily running, receiving nothing. So: remember the hostname we last
# reported, and shout when it differs.
TUNNEL_STATE="$ROOT/data/tunnel-url"

current_tunnel_url() {
    local since out
    if [ "$MODE" = systemd ]; then
        systemctl is-active --quiet kers-tunnel 2>/dev/null || return 0
        # Scope to the current invocation, so a stale URL from a previous
        # run of the tunnel is never reported as live.
        since="$(systemctl show kers-tunnel -p ActiveEnterTimestamp --value 2>/dev/null || true)"
        if [ -n "$since" ]; then
            out="$(journalctl -u kers-tunnel --since "$since" --no-pager 2>/dev/null || true)"
        else
            out="$(journalctl -u kers-tunnel --no-pager 2>/dev/null || true)"
        fi
    else
        [ -f logs/cloudflared.out ] || return 0
        [ -f cloudflared.pid ] || return 0
        ps -p "$(cat cloudflared.pid)" >/dev/null 2>&1 || return 0
        out="$(cat logs/cloudflared.out)"
    fi
    printf '%s\n' "$out" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
}

report_tunnel() {
    local url prev endpoint
    url="$(current_tunnel_url)"
    prev=""
    if [ -f "$TUNNEL_STATE" ]; then prev="$(cat "$TUNNEL_STATE")"; fi

    if [ -z "$url" ]; then
        say "Tunnel"
        if [ "$MODE" = systemd ] && systemctl is-active --quiet kers-tunnel 2>/dev/null; then
            info "running, but no quick-tunnel hostname in its log"
            info "(a named tunnel keeps a fixed hostname — nothing to re-register)"
        else
            info "not running — the helpdesk cannot reach this engine"
            info "start it:  sudo systemctl start kers-tunnel"
        fi
        return 0
    fi

    endpoint="$url/webhooks/zendesk/events"

    if [ "$url" = "$prev" ]; then
        say "Tunnel unchanged"
        info "$endpoint"
        info "already registered in the helpdesk — no action needed"
        return 0
    fi

    printf '\n\033[33m==> TUNNEL URL CHANGED — UPDATE THE HELPDESK WEBHOOK\033[0m\n'
    if [ -n "$prev" ]; then info "was  $prev"; else info "was  (never recorded)"; fi
    info "now  $url"
    echo
    info "Set the helpdesk webhook endpoint to:"
    printf '\n      \033[1m%s\033[0m\n\n' "$endpoint"
    info "Until you do, no webhook arrives and no ticket is scored —"
    info "and nothing on this host will look wrong."
    mkdir -p "$(dirname "$TUNNEL_STATE")"
    printf '%s\n' "$url" > "$TUNNEL_STATE"
}

if [ "$CHECK_TUNNEL" -eq 1 ]; then
    report_tunnel
    exit 0
fi

# Uncommitted tracked changes would be clobbered or would block the pull.
# .env, data/, logs/ and ticket_files/ are gitignored and unaffected.
if ! git diff --quiet || ! git diff --cached --quiet; then
    git status --short
    die "uncommitted changes to tracked files. Commit, stash or discard them first."
fi

# --- what is incoming ------------------------------------------------------
say "Fetching"
git fetch --quiet origin "$BRANCH"

OLD_SHA="$(git rev-parse HEAD)"
NEW_SHA="$(git rev-parse "origin/$BRANCH")"

# "The SHAs differ" is not the same as "there is something to pull": a
# checkout with unpushed local commits is ahead of origin, and a merge
# --ff-only there is a silent no-op that would look like a real deploy.
HAVE_UPDATE=0
if [ "$OLD_SHA" = "$NEW_SHA" ]; then
    info "already at $(git rev-parse --short HEAD) — nothing to pull"
elif git merge-base --is-ancestor "$NEW_SHA" "$OLD_SHA"; then
    info "local branch is $(git rev-list --count "$NEW_SHA..$OLD_SHA") commit(s) ahead of origin/$BRANCH — nothing to pull"
elif git merge-base --is-ancestor "$OLD_SHA" "$NEW_SHA"; then
    HAVE_UPDATE=1
    info "$(git rev-parse --short "$OLD_SHA") -> $(git rev-parse --short "$NEW_SHA")"
    echo
    git --no-pager log --oneline --no-decorate "$OLD_SHA..$NEW_SHA" | sed 's/^/    /'
else
    die "local branch has diverged from origin/$BRANCH. Resolve by hand."
fi

if [ "$HAVE_UPDATE" -eq 0 ] && [ "$FORCE" -ne 1 ]; then
    say "Up to date. Nothing to pull."
    # Still worth checking: the tunnel hostname drifts independently of the
    # code, and this is the command people actually run.
    report_tunnel
    exit 0
fi
if [ "$HAVE_UPDATE" -eq 0 ]; then
    info "--force given, redeploying the current revision anyway"
fi

REQS_CHANGED=0
if [ "$HAVE_UPDATE" -eq 1 ] &&
   ! git diff --quiet "$OLD_SHA" "$NEW_SHA" -- requirements.txt; then
    REQS_CHANGED=1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    info "dry run — no changes made"
    if [ "$REQS_CHANGED" -eq 1 ]; then
        info "requirements.txt changed; a real run would reinstall deps"
    fi
    report_tunnel
    exit 0
fi

# --- snapshot the database -------------------------------------------------
# Cheap insurance. The schema is CREATE TABLE IF NOT EXISTS with no migration
# step, so a bad revision cannot migrate the data — but a copy costs
# milliseconds and this project has lost hosts before.
DB="${ERS_DATA_DIR:-$ROOT/data}/escalation.sqlite"
if [ -f "$DB" ]; then
    say "Snapshotting the database"
    BACKUP_DIR="$ROOT/data/backups"
    mkdir -p "$BACKUP_DIR"
    SNAP="$BACKUP_DIR/escalation-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
    .venv/bin/python - "$DB" "$SNAP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)          # consistent even with the engine mid-write
PY
    info "$(basename "$SNAP")"
    # Keep the ten most recent; these are local copies, not a backup strategy.
    ls -1t "$BACKUP_DIR"/escalation-*.sqlite 2>/dev/null | tail -n +11 |
        while IFS= read -r old; do rm -f "$old"; done
fi

# --- update ----------------------------------------------------------------
if [ "$HAVE_UPDATE" -eq 1 ]; then
    say "Pulling"
    git merge --ff-only --quiet "origin/$BRANCH" ||
        die "fast-forward failed — the local branch has diverged from origin/$BRANCH"
    info "now at $(git rev-parse --short HEAD)"
fi

install_deps() {
    say "Installing dependencies"
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
    info "done"
}
if [ "$REQS_CHANGED" -eq 1 ]; then install_deps; fi

# --- test ------------------------------------------------------------------
if [ "$SKIP_TESTS" -eq 1 ]; then
    say "Skipping tests (--skip-tests)"
else
    say "Running tests"
    if ! .venv/bin/pytest -q; then
        # Nothing has been restarted yet, so the running engine is still on
        # the old revision and still serving. Put the tree back and stop.
        git reset --hard --quiet "$OLD_SHA"
        if [ "$REQS_CHANGED" -eq 1 ]; then install_deps; fi
        die "tests failed. Working tree reset to $(git rev-parse --short "$OLD_SHA"); the running engine was not touched.
       If this is the known timing-sensitive failure on a loaded host
       (test_close_webhook_purges — see deploy/DEPLOY.md), re-run with --skip-tests."
    fi
fi

if [ "$NO_RESTART" -eq 1 ]; then
    say "Updated, not restarting (--no-restart)"
    info "the engine is still running the old code until you restart it"
    report_tunnel
    exit 0
fi

# --- restart ---------------------------------------------------------------
# Never propagates failure: a restart that fails is precisely when the
# rollback below has to run, and under `set -e` a non-zero return here would
# abort the script before reaching it. wait_healthy is the real verdict.
restart_engine() {
    if [ "$MODE" = systemd ]; then
        sudo systemctl restart kers-engine || true
    else
        ./deploy/stop.sh >/dev/null 2>&1 || true
        ./deploy/start.sh >/dev/null 2>&1 || true
    fi
    return 0
}

wait_healthy() {
    local code
    for _ in $(seq 1 30); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
        [ "$code" = "200" ] && return 0
        sleep 1
    done
    return 1
}

say "Restarting the engine"
# A restart drops per-ticket debounce timers that have not yet fired, so a
# ticket mid-debounce is not scored until its next message. Webhooks arriving
# during the ~2s restart are retried by the helpdesk.
restart_engine

say "Waiting for health"
if wait_healthy; then
    info "healthy on 127.0.0.1:$PORT"
else
    printf '\n\033[31m!! new revision did not become healthy — rolling back\033[0m\n' >&2
    if [ "$MODE" = systemd ]; then
        journalctl -u kers-engine -n 30 --no-pager >&2 || true
    else
        tail -30 logs/uvicorn.out >&2 || true
    fi

    say "Rolling back to $(git rev-parse --short "$OLD_SHA")"
    git reset --hard --quiet "$OLD_SHA"
    if [ "$REQS_CHANGED" -eq 1 ]; then install_deps; fi
    restart_engine
    if wait_healthy; then
        die "deploy failed; rolled back to $(git rev-parse --short HEAD) and the engine is healthy again."
    fi
    die "deploy failed AND the rollback did not come up. The engine is DOWN.
       Investigate now: systemctl status kers-engine; journalctl -u kers-engine -n 50"
fi

# --- report ----------------------------------------------------------------
say "Deployed"
info "revision  $(git rev-parse --short HEAD)  $(git log -1 --pretty=%s)"
info "prompt    $(.venv/bin/python -c 'from app.config import PROMPT_VERSION; print(PROMPT_VERSION)')"
info "health    200 on 127.0.0.1:$PORT"

# The tunnel was not restarted by this deploy, but its hostname may have
# drifted since it was last registered — a reboot or a cloudflared
# crash-restart is enough. Report it either way.
report_tunnel
