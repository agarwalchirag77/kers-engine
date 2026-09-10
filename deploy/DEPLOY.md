# Deploying KERS to a fresh Ubuntu host

Written from four real rebuilds. The gotchas at the bottom are all things
that actually broke a deployment, not hypotheticals.

Target: Ubuntu 20.04 or newer. Takes about fifteen minutes, of which one
command needs `sudo`.

---

## 0. What the repo does not contain

Deliberately gitignored, so a fresh clone is not a running system:

| Missing | Why | How to get it |
|---|---|---|
| `.env` | credentials | create it — step 3 |
| `data/escalation.sqlite` | customer conversation content | created empty on first run, or restore a backup |
| `ticket_files/` | customer conversations | `scripts/restore_ticket_files.py` — step 7 |
| `logs/` | customer content | created on first run |
| `scripts/sample_conversations/` | real tickets | only needed by `simulate.py` |

---

## 1. Python 3.9

Ubuntu 20.04 ships Python 3.8, which is **too old** — the pydantic 2.x stack
requires 3.9+. This is the only step needing root.

```bash
sudo apt update && sudo apt install -y \
    python3.9 python3.9-venv python3.9-dev sqlite3 curl git
python3.9 --version          # expect 3.9.x
```

3.9 is in the standard Ubuntu 20.04 repos. No PPA required.

## 2. Clone and build

```bash
cd ~
git clone https://github.com/<owner>/kers-engine.git
cd kers-engine

python3.9 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

.venv/bin/pytest -q
```

**Expect one failure**: `test_close_webhook_purges` is timing-sensitive and
fails on slower hosts. Everything else must pass. If you see other failures,
stop and investigate.

## 3. Credentials

```bash
nano ~/kers-engine/.env
```

```
OPENAI_API_KEY=sk-proj-...
ZENDESK_API_TOKEN=...
ZENDESK_WEBHOOK_SECRET=
ERS_DATA_DIR=/home/<user>/kers-engine/data
ERS_TICKET_DIR=/home/<user>/kers-engine/ticket_files
ERS_LOG_DIR=/home/<user>/kers-engine/logs
```

The engine listens on **port 8080** by default. To change it, add
`ERS_PORT=<n>` to `.env` — `start.sh`, `tunnel.sh` and both systemd units
all read that one variable, so the tunnel always follows the engine rather
than being configured separately and drifting.

**Leave `ZENDESK_WEBHOOK_SECRET` empty unless the helpdesk is actually
signing requests.** If a value is set and the helpdesk does not sign with
that exact secret, every webhook is rejected with 401 and scoring stops
silently. See gotcha 1.

```bash
chmod 600 ~/kers-engine/.env
set -a; . ~/kers-engine/.env; set +a
echo "OPENAI ${#OPENAI_API_KEY} | ZENDESK ${#ZENDESK_API_TOKEN} | SECRET ${#ZENDESK_WEBHOOK_SECRET}"
```

Expect roughly `OPENAI 164 | ZENDESK 40 | SECRET 0`.

## 4. Start the engine

```bash
chmod +x deploy/*.sh
./deploy/start.sh
```

Prints the pid, a health code (expect `200`) and the active prompt version.

## 5. Install cloudflared

Userspace, no root:

```bash
mkdir -p ~/bin
curl -fsSL -o ~/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/bin/cloudflared
~/bin/cloudflared --version
```

On arm64, swap `amd64` for `arm64` in that URL.

## 6. Open the tunnel

```bash
./deploy/tunnel.sh
```

Prints the webhook endpoint. **Paste it into the helpdesk's webhook config.**
Set authentication to None if `ZENDESK_WEBHOOK_SECRET` is empty.

Verify from somewhere off the host:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<subdomain>.trycloudflare.com/health
```

## 7. Populate conversations

A fresh install has no conversation history, so no ticket can reach the
six-message threshold until six new messages arrive. To seed from the
helpdesk:

```bash
set -a; . ~/kers-engine/.env; set +a
.venv/bin/python scripts/restore_ticket_files.py --dry-run
.venv/bin/python scripts/restore_ticket_files.py
```

Optionally score them, which costs real money — always dry-run first:

```bash
.venv/bin/python scripts/backfill_scores.py --dry-run   # prints scope and cost
.venv/bin/python scripts/backfill_scores.py
```

## 8. Verify end to end

```bash
grep -c "POST /webhooks/zendesk/events" logs/uvicorn.out          # inbound
grep -c 'events HTTP/1.1" 200'          logs/uvicorn.out          # accepted
sqlite3 data/escalation.sqlite \
  "SELECT prompt_version, COUNT(*), MAX(evaluated_at) FROM ers_events GROUP BY 1;"
tail -f logs/engine-$(date -u +%Y-%m-%d).jsonl                    # live events
```

A healthy ticket walks through: `webhook_received` → `message_appended` →
`debounce_scheduled` → `ai_called` → `ers_computed` → `pushed_to_zd` →
`db_written`.

## 9. Back up off the host

The single most important step, and the easiest to skip. Three hosts have
been lost to expiry so far; the one with backups lost nothing.

From a machine that is *not* the server, on a schedule:

```bash
ssh -p <port> <user>@<host> \
  "cd ~/kers-engine && tar -czf /tmp/kers-snap.tar.gz data/escalation.sqlite ticket_files/"
scp -P <port> <user>@<host>:/tmp/kers-snap.tar.gz ./backups/$(date +%F-%H%M).tar.gz
```

Create the local dated directory **only after** the transfer succeeds —
otherwise a dead server leaves empty folders that look like good backups.

---

## Gotchas

Each of these broke a real deployment.

**1. A wrong webhook secret looks exactly like silence.**
A stale `ZENDESK_WEBHOOK_SECRET` made every webhook 401 for fourteen hours.
Nothing errored; the engine was healthy and simply received nothing valid.
If scoring stops, check `grep -c '401' logs/uvicorn.out` first.

**2. `pkill -f` kills your own SSH session.**
`pkill -f cloudflared` matches the command line of the shell you typed it
in. Use the pidfiles, or `pgrep -x` / `killall` against the executable name.

**3. cloudflared self-terminates on auto-update.**
It replaces its binary and exits, expecting an init system to restart it.
Under plain `nohup` nothing does. Always pass `--no-autoupdate`; `tunnel.sh`
does.

**4. The tunnel hostname changes on every restart.**
Quick tunnels are random. Every restart means repointing the helpdesk
webhook. For anything long-lived, use a named tunnel with a fixed hostname.

**5. Python 3.8 is not enough.**
The stack needs 3.9+. Installing it is the only root step.

**6. Always `--dry-run` the scripts that call the model.**
`backfill_scores.py` and `replay_gap.py` print scope and estimated cost with
`--dry-run`. A replay that looked like $2 once turned out to be $6 when the
plan logic changed. Check the number before spending.

**7. Nothing tells you when the model API stops working.**
An exhausted API balance returns HTTP 429 — the same status as rate
limiting, with a different `code`. Webhooks keep arriving, messages keep
being stored, and no score is ever produced. Check
`grep -c ai_response_invalid logs/engine-*.jsonl` periodically.

## Operating

```bash
./deploy/start.sh          # start engine
./deploy/stop.sh           # stop engine, leave tunnel up
./deploy/stop.sh --all     # stop both
./deploy/tunnel.sh         # start tunnel, print the URL
```

Restarting the engine takes about two seconds. Webhooks that land in that
window are retried by the helpdesk, so a restart during normal traffic is
safe.

---

## Keeping it alive properly (systemd)

`start.sh` uses `nohup`, so the engine survives you closing the terminal or
logging out. It does **not** survive a reboot, and nothing restarts it if it
crashes. Three hosts have been lost so far and in each case the service
simply stopped existing.

For anything that matters, install the units instead. This is one root step
and then you can stop thinking about it.

```bash
cd ~/kers-engine

# Point the units at this host: replace the user and paths if not khushi.s
sed -i "s|khushi.s|$USER|g" deploy/systemd/*.service

sudo cp deploy/systemd/kers-engine.service /etc/systemd/system/
sudo cp deploy/systemd/kers-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now kers-engine
sudo systemctl enable --now kers-tunnel
```

`enable` is what makes it come back after a reboot; `--now` starts it
immediately.

```bash
systemctl status kers-engine
journalctl -u kers-engine -f          # live logs
journalctl -u kers-tunnel | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
sudo systemctl restart kers-engine    # e.g. after changing a prompt
```

What the units give you over `nohup`:

| | nohup | systemd |
|---|---|---|
| survives closing the terminal | yes | yes |
| survives logout | yes | yes |
| survives a crash | **no** | yes, `Restart=always` |
| survives a reboot | **no** | yes, via `enable` |
| log rotation | no, `uvicorn.out` grows forever | journald handles it |
| credentials | `source`d into the shell | read by systemd, never in the process list |

Do not run both at once — stop the `nohup` copies first with
`./deploy/stop.sh --all`, or you will have two engines fighting over port
the port and the second will fail to bind.

### The tunnel caveat

`kers-tunnel.service` will faithfully restart cloudflared, but a **quick
tunnel gets a new random hostname every restart**. The process comes back,
the webhook stays broken, and nothing on this host looks wrong.

Auto-restart only fully works with a **named tunnel**, which keeps a fixed
hostname:

```bash
~/bin/cloudflared tunnel login
~/bin/cloudflared tunnel create kers
~/bin/cloudflared tunnel route dns kers kers.<your-domain>
```

then change `ExecStart` in the unit to:

```
ExecStart=/home/<user>/bin/cloudflared tunnel --no-autoupdate run kers
```

After that the webhook URL never changes again, and the helpdesk never needs
repointing. This is the single change that would have prevented most of the
outages this service has had.
