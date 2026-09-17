# KERS Handover

Everything needed to take over this service: run it locally, change it, deploy
it, and know when it has silently stopped working.

Host-specific details — the server address, its filesystem paths, and the
current webhook configuration — are deliberately **not** in this file. This
repository is public. Ask the current owner for the infrastructure notes.

- [What it does](#what-it-does)
- [The score](#the-score)
- [Repo layout](#repo-layout)
- [Local setup](#local-setup)
- [Deploying to a server](#deploying-to-a-server)
- [Deploying a change](#deploying-a-change)
- [The tunnel problem](#the-tunnel-problem)
- [Monitoring](#monitoring)
- [Operational scripts](#operational-scripts)
- [Traps](#traps)
- [Open items](#open-items)

---

## What it does

KERS reads support ticket conversations as they happen, scores how likely each
is to escalate, and writes that number back onto the ticket so the helpdesk's
own trigger system can act on it. It does not message customers and does not
assign work. It produces one number per ticket.

```
new comment in the helpdesk
        │
        ▼
  POST /webhooks/zendesk/events     signature check (skipped if no secret)
        ▼
  adapter                           helpdesk event → internal model
        ▼
  filter                            allowed groups, excluded tags
        ▼
  append                            ticket_files/<id>.json
        ▼
  debounce 60s                      5 messages in a burst = 1 evaluation
        ▼
  threshold                         skip entirely under 6 messages
        ▼
  LLM                               temperature 0, fixed seed → 8 sub-scores
        ▼
  weighted sum                      ERS, 0–4
        ├──▶ PUT   the ticket's ERS custom field
        └──▶ INSERT data/escalation.sqlite
```

### The one constraint that shapes everything

This is a **stateful singleton**. One instance, one host, one persistent disk.
Never two replicas, never an ephemeral container filesystem. Four independent
reasons:

| State | Where | What breaks with two instances |
|---|---|---|
| Conversation history | `ticket_files/` on local disk | Messages split across instances; neither ticket reaches the 6-message threshold |
| Evaluations | `data/escalation.sqlite` | SQLite is single-writer, local-file only |
| Debounce timers | `Worker._debounce_tasks`, in memory | A restart drops pending evaluations — that ticket isn't scored until its next message |
| Event dedup | `Worker._seen_events`, in memory | Cleared on restart, so replayed webhooks are re-processed |

---

## The score

ERS is a weighted sum of eight sub-scores, each 0–4. **Seven measure the
customer**; only `agent_tone` measures the agent. Weights live in
`app/config/weights.json` and must sum to 1.00.

| Sub-score | Weight | Measures |
|---|--:|---|
| `churn_threat` | 0.25 | Explicit or implicit intent to leave |
| `frustration` | 0.15 | Expressed anger or dissatisfaction |
| `dissatisfaction_trajectory` | 0.13 | Direction of sentiment across the ticket |
| `urgency` | 0.12 | Time pressure the customer signals |
| `commitment_to_delivery_ratio` | 0.12 | Agent promises missed vs kept — computed deterministically, not by the model |
| `confusion` | 0.10 | How well the customer understands their situation |
| `agent_tone` | 0.08 | Agent professionalism — the only agent-facing score |
| `politeness_erosion` | 0.05 | Decline in civility against the customer's own baseline |

Threshold 3.25 · debounce 60s · minimum 6 messages before evaluation.

### Two design decisions to preserve

**Severity is not distress.** Earlier prompts read "production is down" as
anger. The current prompt carries anchor tables for each band of each metric,
including a *"what does NOT qualify"* list. A severe outage described in flat,
factual language scores near zero on frustration. Don't undo this by
simplifying the prompt.

**Commitments are timed by the engine, not the model.** `app/commitments.py`
detects promises, computes deadlines against the real evaluation time, and
classifies each as delivered, missed or pending. The model is shown the result
and told not to recompute it — an LLM has no reliable sense of what time it is
now. `ai_client.py` then overrides the model's commitment fields with the
engine's values.

### Reproducibility

`temperature=0` with a fixed seed (`KERS_SEED` in `app/ai_client.py`) makes
scoring reproducible run to run. Before this, temperature 0.2 with no seed
produced a 2.25-point ERS spread on the same ticket across two runs.

> **Changing the seed re-randomises every future score.** Scores stop being
> comparable with everything already in the database. Same for switching
> prompt version without intent.

Prompts are versioned files in `app/config/prompts/`. Every stored evaluation
records which version produced it, so scores stay comparable within a version
and are never silently mixed across one. Switch by editing `PROMPT_VERSION` in
`app/config.py`.

---

## Repo layout

Everything helpdesk-specific lives under `connectors/`. Nothing in
`worker.py`, `ers.py`, `commitments.py` or `storage/` is platform-specific —
adding another helpdesk means adding a sibling connector.

| Path | What |
|---|---|
| `app/main.py` | FastAPI app; builds the worker at lifespan startup |
| `app/webhook.py` | Inbound routes, HMAC verification, payload archiving, filtering |
| `app/worker.py` | Async queue, per-ticket debounce and locking, orchestration |
| `app/ai_client.py` | LLM call, JSON response validation, one retry |
| `app/ers.py` | The weighted sum. Small — read it first |
| `app/commitments.py` | Deterministic promise detection and deadline timing |
| `app/models.py` | Pydantic domain models and response validation rules |
| `app/config/` | `weights.json`, versioned prompt files |
| `app/connectors/zendesk/` | `adapter.py`, `pusher.py`, `settings.json`, `filters.json` |
| `app/storage/` | SQLite writer, per-ticket JSON store, JSONL structured logger |
| `deploy/` | Runbook, start/stop/tunnel/update scripts, systemd unit templates |
| `scripts/` | Operational tools — see below |

Helpdesk instance configuration — subdomain, custom field id, API user — lives
in `app/connectors/zendesk/settings.json`. Group and tag scope lives in
`filters.json`.

> The API token in `.env` must belong to the `api_user` named in
> `settings.json`. If it doesn't, the engine scores normally and *every push
> 401s* — visible only as `push_failed` in the JSONL log. That user is
> currently a named individual, not a service account; see open items.

Chat (messaging) tickets deliberately **bypass the group whitelist**. Only
excluded tags can stop a chat ticket from being processed.

---

## Local setup

Python 3.9 or newer. The pydantic 2.x stack will not run on 3.8.

```bash
git clone https://github.com/agarwalchirag77/kers-engine.git
cd kers-engine
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
```

All 148 tests should pass. `test_close_webhook_purges` is timing-sensitive and
can fail on a loaded machine; any other failure means something is genuinely
wrong.

Create a `.env` at the repo root — gitignored. For local work the keys can be
dummies unless you're exercising a real model call.

```
OPENAI_API_KEY=sk-...
ZENDESK_API_TOKEN=...
ZENDESK_WEBHOOK_SECRET=
ERS_PORT=8080
```

Optional overrides: `ERS_MODEL`, `ERS_DATA_DIR`, `ERS_TICKET_DIR`,
`ERS_LOG_DIR`. `ERS_PORT` is read by both deploy scripts and both systemd
units, so setting it once moves the engine and the tunnel together.

Run it in the foreground:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 \
  --log-config uvicorn_log_config.json

curl -s localhost:8080/health   # {"status":"ok"}
```

`--log-config` adds timestamps to access logs, which the uvicorn default does
not. Binding `127.0.0.1` is deliberate: with no webhook secret set there is no
signature check, so `0.0.0.0` would expose an unauthenticated endpoint.

### Testing a change without the helpdesk

`scripts/simulate.py` replays a saved conversation offline. It reads
`calm.json` and `escalating.json` from `scripts/sample_conversations/`, which
are **not in this repository** — the working copies are real tickets
containing customer names. Supply your own:

```json
[
  {
    "author_type": "customer",
    "author_id": "12345",
    "body": "Our pipeline stopped syncing this morning.",
    "timestamp": "2026-01-15T09:14:00Z"
  }
]
```

`author_type` is `customer` or `agent`; `author_id` is optional.

---

## Deploying to a server

Full runbook: [deploy/DEPLOY.md](deploy/DEPLOY.md). Summary:

1. Clone, build the venv, run the tests.
2. Create `.env` with absolute `ERS_*` paths, then `chmod 600 .env`.
3. `./deploy/install-systemd.sh` — renders the unit templates for this host.
4. `sudo systemctl enable --now kers-engine`.
5. Verify the rendered paths before trusting it:
   `systemctl cat kers-engine | grep -E 'WorkingDirectory|ExecStart|EnvironmentFile'`
6. Install cloudflared and bring up the tunnel, then register the webhook URL
   in the helpdesk.
7. Seed conversation history with `scripts/restore_ticket_files.py`.

**Never edit the unit files in the checkout.** They are templates containing
`__KERS_ROOT__`, `__KERS_USER__` and `__CLOUDFLARED__` placeholders.
`install-systemd.sh` derives all three from wherever the repo actually is,
which keeps the checkout clean and future pulls conflict-free.

**`EnvironmentFile` is not bash.** No `export` prefixes, no shell quoting —
just `KEY=value`. An `export FOO=bar` line makes systemd create a variable
literally named `export FOO`, and the engine sees no key.

Step 7 matters more than it looks. A fresh install has no conversation
history, so no ticket can reach six messages until six *new* ones arrive —
which looks exactly like a broken service for days.

---

## Deploying a change

One command. It fetches, snapshots the database, fast-forwards, reinstalls
dependencies only if `requirements.txt` changed, runs the tests, restarts the
engine, and waits for health.

```bash
./deploy/update.sh --dry-run   # what would be pulled
./deploy/update.sh             # do it
```

| Flag | Effect |
|---|---|
| `--dry-run` | Show incoming commits, change nothing |
| `--force` | Redeploy the current revision even with nothing to pull |
| `--skip-tests` | Skip pytest — for the known flaky timing test |
| `--no-restart` | Update the working tree only |
| `--check-tunnel` | Print the current webhook URL and exit |

What protects you:

- **Tests run before any restart.** A failing revision never reaches the
  running service — the tree is reset and the engine is left untouched.
- **Automatic rollback.** If the restarted engine doesn't return 200 within
  30s, the failure logs are dumped, the tree is reset to the previous commit,
  dependencies are reinstalled if they'd changed, and it restarts and
  re-verifies. If *that* also fails it says so loudly rather than exiting
  cleanly.
- **It refuses a dirty checkout.** Deliberate — it will not silently discard
  your edits. Commit, stash, or `git checkout --` them.
- **It never restarts the tunnel.** See below.

A restart takes about two seconds. Webhooks landing in that window are retried
by the helpdesk, so restarting during normal traffic is safe. What *is* lost:
per-ticket debounce timers that haven't fired yet.

---

## The tunnel problem

The engine binds loopback only, so something must front it. `deploy/tunnel.sh`
and `kers-tunnel.service` use a Cloudflare **quick tunnel**.

> A quick tunnel gets a **new random hostname every time cloudflared
> restarts** — a reboot, a crash-restart, an auto-update. systemd faithfully
> brings the process back, the helpdesk stays pointed at the dead hostname,
> and *nothing on the host looks wrong*: health is 200, the process is up, and
> no webhook ever arrives. This has cost a full day of dropped webhooks
> before.

`--no-autoupdate` is mandatory and the unit sets it. Without it cloudflared
replaces its own binary once a day and exits — on a quick tunnel that means a
new hostname at a random hour with nobody watching.

`update.sh` records the hostname it last reported and shouts when it changes,
on every run including `--dry-run`:

```
==> TUNNEL URL CHANGED — UPDATE THE HELPDESK WEBHOOK
    was  https://old-name-here.trycloudflare.com
    now  https://new-name-here.trycloudflare.com

    Set the helpdesk webhook endpoint to:

      https://new-name-here.trycloudflare.com/webhooks/zendesk/events
```

It reports the change; it cannot confirm you acted on it. Check after every
restart:

```bash
./deploy/update.sh --check-tunnel
```

### The fix worth making

Two options remove the problem instead of reporting it:

- **Direct exposure** — a real DNS name and TLS (Caddy or nginx), no
  cloudflared. Permanent URL. Then set a real `ZENDESK_WEBHOOK_SECRET`:
  verification is skipped entirely while it is empty, which is defensible
  behind an unguessable tunnel hostname and not defensible on a public name.
- **A named tunnel** — needs a Cloudflare-managed domain, keeps a fixed
  hostname, exposes no ports. `update.sh` already recognises this case and
  reports nothing to re-register.

Register the webhook as a helpdesk **Event Subscription** subscribed to
`ticket.comment_added` and `ticket.status_changed`.

---

## Monitoring

Every failure mode in this service is quiet. None of them make `/health` go
red.

```bash
journalctl -u kers-engine -f
tail -f logs/engine-$(date -u +%Y-%m-%d).jsonl
```

A healthy ticket walks through these stages in order:

```
webhook_received → message_appended → debounce_scheduled
                 → ai_called → ers_computed → pushed_to_zd → db_written
```

### The four silent failures

| Symptom | Cause | Check |
|---|---|---|
| No webhooks arriving | Tunnel hostname drifted after a restart | `./deploy/update.sh --check-tunnel` |
| Everything 401s | `ZENDESK_WEBHOOK_SECRET` set but the helpdesk isn't signing with it | `grep -c '401' logs/uvicorn.out` |
| Scores computed, never appear on tickets | API token doesn't belong to the configured `api_user` | `grep -c push_failed logs/engine-*.jsonl` |
| Webhooks arrive, no score ever produced | Model API balance exhausted — returns 429, the same status as rate limiting | `grep -c ai_response_invalid logs/engine-*.jsonl` |

### Is it actually scoring?

```bash
sqlite3 data/escalation.sqlite \
  "SELECT prompt_version, COUNT(*), MAX(evaluated_at) FROM ers_events GROUP BY 1;"

# zero on a weekday means the webhook is broken
grep -c webhook_received logs/engine-$(date -u +%Y-%m-%d).jsonl
```

There is a `tickets_summary` view for casual browsing — one row per ticket
with latest score and lifetime stats. Use `ers_events` for full history.

### Data it keeps

| Path | Contents |
|---|---|
| `data/escalation.sqlite` | One row per evaluation: score, sub-score breakdown, model reasoning, prompt version, push outcome |
| `ticket_files/*.json` | The conversation per ticket, deleted when the ticket closes |
| `logs/engine-*.jsonl` | Structured events; previous days purged automatically on the first write of a new UTC day |

All three contain customer conversation content and are gitignored.
`update.sh` keeps ten local database snapshots in `data/backups/` — that is
deploy insurance, **not a backup**.

### Real backups

Three hosts have been lost to expiry on this project; the one with backups
lost nothing. From a machine that is *not* the server, on a schedule:

```bash
ssh <user>@<host> "cd ~/kers-engine && \
  tar -czf /tmp/kers-snap.tar.gz data/escalation.sqlite ticket_files/"
scp <user>@<host>:/tmp/kers-snap.tar.gz ./kers-$(date +%F-%H%M).tar.gz
```

Create the local dated directory only *after* the transfer succeeds —
otherwise a dead server leaves empty folders that look like good backups.

---

## Operational scripts

Most take `--dry-run`, which reports scope and estimated cost without making
any model calls. Use it — a replay that looked like $2 once turned out to be
$6 when the plan logic changed.

| Script | Purpose | Costs money |
|---|---|:--:|
| `restore_ticket_files.py` | Rebuild the conversation store from the helpdesk API | no |
| `backfill_scores.py` | Score existing tickets that have no evaluation yet | **yes** |
| `replay_gap.py` | After an outage, score every missed message, backdated to arrival | **yes** |
| `validate_anchors.py` | Score 20 labelled examples, report band accuracy and run-to-run spread | **yes** |
| `simulate.py` | Replay a saved conversation offline | **yes** |
| `backfill_author_ids.py` | Recover per-message author identity, build a local user directory | no |
| `repair_author_types.py` | Re-derive customer-vs-agent labels from that directory | no |

Run them with the environment loaded: `set -a; . ./.env; set +a`.

`validate_anchors.py` is the one to run after any prompt change — it tells you
whether the new prompt still lands scores in the right bands, and how much
they move between runs.

---

## Traps

Each of these broke a real deployment. More detail in
[deploy/DEPLOY.md](deploy/DEPLOY.md#gotchas).

**Wrong unit paths.** Cost six days of downtime. The old install recipe was
`sed -i "s|khushi.s|$USER|g"`, which fixed only the username and left every
path as `/home/<user>/kers-engine` — wrong for any checkout not sitting
directly in `$HOME`. Now fixed by `install-systemd.sh`. If a unit won't start,
read the first two journal lines: `Failed to load environment files` means
`EnvironmentFile` is wrong; `Failed to spawn 'start' task` means `ExecStart`
is wrong.

**Crash-loop instead of failure.** `StartLimitBurst` and
`StartLimitIntervalSec` are `[Unit]` directives. They were in `[Service]`,
where systemd ignores them with an `Unknown key` warning — so a broken unit
restarted every 5s, 232 times, instead of failing visibly after five
attempts. Fixed.

**A wrong webhook secret looks exactly like silence.** A stale
`ZENDESK_WEBHOOK_SECRET` made every webhook 401 for fourteen hours. Nothing
errored; the engine was healthy and simply received nothing valid.

**`pkill -f` kills your own SSH session.** `pkill -f cloudflared` matches the
command line of the shell you typed it in. Use the pidfiles, or `pgrep -x`
against the executable name. `stop.sh` is deliberately written this way.

**Two engines fighting for the port.** Never run the `nohup` scripts and
systemd at once. Stop the nohup copies first with `./deploy/stop.sh --all`.

**Python 3.8 is not enough.** Ubuntu 20.04 ships 3.8; the pydantic 2.x stack
needs 3.9+.

---

## Open items

Known and deliberately not done, roughly in order of how much they matter.

1. **Pin `requirements.txt`.** Lower bounds only, so a fresh install resolves
   whatever is newest that day — a clean build in Sep 2026 pulled
   `openai 3.14.1`, `fastapi 0.141.1`, `pydantic 2.13.5`. Pin from the
   *server's* `pip freeze`, never from a dev machine, or the next
   `update.sh` will quietly move the working deployment's dependencies.

2. **A permanent webhook URL.** Direct exposure or a named tunnel. Removes the
   single largest source of outages in this service's history.

3. **Decouple from one person's helpdesk account.** `api_user` in
   `settings.json` is a named individual. A service account would survive them
   leaving.

4. **`_seen_events` grows unbounded.** The in-memory dedup set never evicts.
   Harmless at current volume over weeks; worth a bounded structure before a
   long uptime at higher volume.

5. **`restore_ticket_files.py` ignores `ERS_TICKET_DIR`.** It hardcodes
   `<repo>/ticket_files`. Correct when the env var points there — it will
   silently write to the wrong place if anyone moves the store.

6. **No alerting.** Every check above is manual. A daily timer that posts to
   Slack when `webhook_received` is zero would catch the most common failure
   without anyone remembering to look.

---

KERS — Kinetic Escalation Risk Scoring. Originally built by Khushi S; the K is
for Khushi.
