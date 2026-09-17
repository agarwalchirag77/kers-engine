#!/usr/bin/env bash
# Render the systemd unit templates for THIS host and install them.
#
# Usage:
#   sudo -v && ./deploy/install-systemd.sh              engine only
#   sudo -v && ./deploy/install-systemd.sh --with-tunnel  engine + cloudflared
#
# Replaces the old "sed -i s|khushi.s|$USER|g" recipe, which only fixed the
# USERNAME and silently left the paths pointing at /home/<user>/kers-engine.
# Any checkout not sitting directly in $HOME then produced a unit that failed
# with "Failed to load environment files" and "Failed to spawn 'start' task",
# and — because StartLimit* was in the wrong section — crash-looped a few
# hundred times instead of failing visibly. This derives every path from
# where the repo actually is.

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

WITH_TUNNEL=0
for arg in "$@"; do
    case "$arg" in
        --with-tunnel) WITH_TUNNEL=1 ;;
        -h|--help)     awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *)             echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# The unit runs as whoever owns the checkout, not as root.
KERS_USER="$(stat -c '%U' "$ROOT" 2>/dev/null || stat -f '%Su' "$ROOT")"

say "Detected"
info "root   $ROOT"
info "user   $KERS_USER"

[ -f "$ROOT/.env" ]            || die ".env not found at $ROOT/.env. See deploy/DEPLOY.md step 3."
[ -x "$ROOT/.venv/bin/uvicorn" ] || die ".venv not built at $ROOT/.venv. See deploy/DEPLOY.md step 2."
id "$KERS_USER" >/dev/null 2>&1 || die "user '$KERS_USER' does not exist on this host"

render() {
    sed -e "s|__KERS_ROOT__|$ROOT|g" \
        -e "s|__KERS_USER__|$KERS_USER|g" \
        -e "s|__CLOUDFLARED__|${CF:-/usr/local/bin/cloudflared}|g" \
        "$1"
}

say "Installing kers-engine.service"
render deploy/systemd/kers-engine.service | sudo tee /etc/systemd/system/kers-engine.service >/dev/null
info "/etc/systemd/system/kers-engine.service"

if [ "$WITH_TUNNEL" -eq 1 ]; then
    CF="$(command -v cloudflared || true)"
    [ -z "$CF" ] && [ -x "$HOME/bin/cloudflared" ] && CF="$HOME/bin/cloudflared"
    [ -n "$CF" ] || die "cloudflared not found. Install it (deploy/DEPLOY.md step 5) or drop --with-tunnel."
    say "Installing kers-tunnel.service"
    info "cloudflared at $CF"
    render deploy/systemd/kers-tunnel.service | sudo tee /etc/systemd/system/kers-tunnel.service >/dev/null
    info "/etc/systemd/system/kers-tunnel.service"
fi

say "Reloading systemd"
sudo systemctl daemon-reload

say "Installed. Not started."
info "sudo systemctl enable --now kers-engine"
# Not `[ ... ] && info ...`: as the last statement of the script a false test
# would make the whole script exit non-zero.
if [ "$WITH_TUNNEL" -eq 1 ]; then info "sudo systemctl enable --now kers-tunnel"; fi
info "systemctl status kers-engine"
echo
info "Verify the rendered paths before starting:"
info "  systemctl cat kers-engine | grep -E 'WorkingDirectory|ExecStart|EnvironmentFile'"
