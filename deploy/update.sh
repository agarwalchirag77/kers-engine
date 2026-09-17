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

DRY_RUN=0 FORCE=0 SKIP_TESTS=0 NO_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=1 ;;
        --force)      FORCE=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --no-restart) NO_RESTART=1 ;;
        -h|--help)    awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' \
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

# Uncommitted tracked changes would be clobbered or would block the pull.
# .env, data/, logs/ and ticket_files/ are gitignored and unaffected.
if ! git diff --quiet || ! git diff --cached --quiet; then
    git status --short
    die "uncommitted changes to tracked files. Commit, stash or discard them first."
fi

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

# --- what is incoming ------------------------------------------------------
say "Fetching"
git fetch --quiet origin "$BRANCH"

OLD_SHA="$(git rev-parse HEAD)"
NEW_SHA="$(git rev-parse "origin/$BRANCH")"

if [ "$OLD_SHA" = "$NEW_SHA" ]; then
    info "already at $(git rev-parse --short HEAD) — nothing to pull"
    if [ "$FORCE" -ne 1 ]; then
        say "Up to date. Nothing to do."
        exit 0
    fi
    info "--force given, redeploying the current revision anyway"
else
    info "$(git rev-parse --short "$OLD_SHA") -> $(git rev-parse --short "$NEW_SHA")"
    echo
    git --no-pager log --oneline --no-decorate "$OLD_SHA..$NEW_SHA" | sed 's/^/    /'
fi

REQS_CHANGED=0
if [ "$OLD_SHA" != "$NEW_SHA" ] &&
   ! git diff --quiet "$OLD_SHA" "$NEW_SHA" -- requirements.txt; then
    REQS_CHANGED=1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    info "dry run — no changes made"
    if [ "$REQS_CHANGED" -eq 1 ]; then
        info "requirements.txt changed; a real run would reinstall deps"
    fi
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
if [ "$OLD_SHA" != "$NEW_SHA" ]; then
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

if [ "$MODE" = systemd ] && systemctl is-active --quiet kers-tunnel; then
    echo
    info "kers-tunnel was left running and its hostname is unchanged —"
    info "no need to re-register the webhook for this deploy."
fi
