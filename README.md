# KERS — Kinetic Escalation Risk Scoring

K-ERS, where ERS = Escalation Risk Score. The K is for Khushi, who built it.

A service that reads support ticket conversations as they happen, scores how
likely each one is to escalate, and writes that score back onto the ticket so
the helpdesk's own trigger system can act on it.

KERS does not message customers and does not assign work. It produces one
number per ticket and lets the existing workflow decide what to do with it.

---

## How it works

```
new message in the helpdesk
        │
        ▼
  webhook  ──►  adapter  ──►  filter  ──►  60s debounce
                                                │
                                                ▼
                                    conversation → LLM → 8 sub-scores
                                                │
                                       weighted sum = ERS (0–4)
                                                │
                      ┌─────────────────────────┴──────────────┐
                      ▼                                        ▼
          written to a custom field                     stored in SQLite
          on the ticket                                 for analysis
```

The debounce matters: a burst of five messages produces one evaluation, not
five. Evaluation is skipped entirely until a ticket has at least six messages,
since there is little to read before that.

## The score

ERS is a weighted sum of eight sub-scores, each on a 0–4 scale.
**Seven measure the customer**; only `agent_tone` measures the agent.

| Sub-score | Weight | Measures |
|---|--:|---|
| `churn_threat` | 0.25 | explicit or implicit intent to leave |
| `frustration` | 0.15 | the customer's expressed anger or dissatisfaction |
| `dissatisfaction_trajectory` | 0.13 | direction of sentiment across the whole ticket |
| `urgency` | 0.12 | time pressure the customer is signalling |
| `commitment_to_delivery_ratio` | 0.12 | agent promises missed vs kept — computed deterministically, not by the model |
| `confusion` | 0.10 | how well the customer understands their own situation |
| `agent_tone` | 0.08 | agent professionalism (the only agent-facing score) |
| `politeness_erosion` | 0.05 | decline in the customer's civility against their own baseline |

Weights live in `app/config/weights.json` and must sum to 1.00.

### Two design decisions worth knowing

**Severity is not distress.** Earlier prompt versions read "production is down"
as anger. The current prompt carries explicit anchor tables for each band of
each metric, including a *"what does NOT qualify"* list. A severe outage
described in flat, factual language scores near zero on frustration.

**Commitments are timed by the engine, not the model.** `app/commitments.py`
detects promises ("I'll get back to you in 10 minutes"), computes deadlines
against the real evaluation time, and classifies each as DELIVERED, MISSED or
PENDING. The model is shown the result and told not to recompute it. This
exists because an LLM has no reliable sense of what time it is now.

## Prompt versions

Prompts are versioned files in `app/config/prompts/`, and every stored
evaluation records which one produced it, so scores stay comparable within a
version and are never silently mixed across one.

| Version | Change |
|---|---|
| v1–v3 | early iterations; recency semantics for sub-scores |
| v4 | separated "measures the customer" from "measures the agent" |
| v5 | embedded anchor tables, moved every metric from 0–5 to **0–4**, discrete band values |
| v6 | rebuilt the `agent_tone` bands on an observable checklist |

Switch versions by editing `PROMPT_VERSION` in `app/config.py`.

## Layout

```
app/
  main.py                     FastAPI app; wires storage, AI client, worker
  webhook.py                  inbound routes
  worker.py                   async queue, debounce, evaluation orchestration
  ai_client.py                LLM call, response validation, retry
  ers.py                      the weighted sum
  commitments.py              deterministic promise detection and timing
  filtering.py                group and tag filters
  models.py                   pydantic domain models
  config/
    weights.json              weights, threshold, debounce, min messages
    prompts/                  versioned prompt files
  connectors/
    zendesk/                  everything helpdesk-specific lives here
      adapter.py                inbound events → internal models
      pusher.py                 writes the score back
      settings.json             instance config
      filters.json              which groups and tags are in scope
  storage/
    db.py                     SQLite writer for ers_events
    ticket_files.py           per-ticket conversation store
    logger.py                 JSONL structured log, auto-purged daily

scripts/                      operational tools, see below
tests/                        unit and integration tests
```

**Adding another helpdesk** means adding a sibling under `connectors/`. Nothing
in `worker.py`, `ers.py`, `commitments.py` or `storage/` is platform-specific.

## Setup

```bash
python3.9 -m venv .venv          # 3.9 or newer
.venv/bin/pip install -r requirements.txt
```

Three environment variables are required:

```bash
export OPENAI_API_KEY=sk-...
export ZENDESK_API_TOKEN=...
export ZENDESK_WEBHOOK_SECRET=      # leave empty to skip signature checks
```

See `deploy/secrets.env.example`. Optional overrides: `ERS_MODEL`,
`ERS_DATA_DIR`, `ERS_TICKET_DIR`, `ERS_LOG_DIR`.

## Running

For a server, install the systemd units — they survive a closed terminal, a
logout, a crash and a reboot. See [deploy/DEPLOY.md](deploy/DEPLOY.md).

```bash
./deploy/start.sh          # background, writes a pidfile, prints health
./deploy/tunnel.sh         # tunnel + the webhook URL to register
./deploy/stop.sh --all     # stop both
```

In the foreground, for local development only:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  --log-config uvicorn_log_config.json
```

Point your helpdesk's webhook at `POST /webhooks/zendesk/events`.
`--log-config` adds timestamps to access logs, which the default does not.
Binding `127.0.0.1` is deliberate — with no webhook secret set there is no
signature check, so `0.0.0.0` would expose an unauthenticated endpoint.

If the webhook secret is set, requests are verified by HMAC-SHA256 and
unsigned ones are rejected. If it is empty, verification is skipped — fine for
development, not for anything exposed.

## Tests

```bash
.venv/bin/pytest -q
```

## Scripts

| Script | Purpose |
|---|---|
| `validate_anchors.py` | scores 20 labelled examples against the anchor tables and reports band accuracy and run-to-run spread |
| `replay_gap.py` | after an outage, scores every message the engine missed, backdated to when it arrived |
| `backfill_scores.py` | scores existing tickets that have no evaluation yet |
| `restore_ticket_files.py` | rebuilds the conversation store from the helpdesk API |
| `backfill_author_ids.py` | recovers per-message author identity and builds a local user directory |
| `repair_author_types.py` | re-derives customer-vs-agent labels from that directory |
| `simulate.py` | replays a saved conversation offline |

Most take `--dry-run`, which reports scope and estimated cost without making
any model calls. Use it.

### A note on `scripts/sample_conversations/`

`simulate.py` reads two files from that directory, `calm.json` and
`escalating.json`. They are **not in this repository** — the working copies are
real support tickets containing customer names and real ticket numbers, so they
are gitignored.

To use the simulator, supply your own. The format is a JSON array of messages:

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

## Data it keeps

- `data/escalation.sqlite` — one row per evaluation: score, sub-score
  breakdown, the model's reasoning, prompt version, push outcome
- `ticket_files/*.json` — the conversation per ticket, deleted when the ticket
  closes
- `logs/engine-YYYY-MM-DD.jsonl` — structured events, previous days purged
  automatically

All three are gitignored. They contain customer conversation content.
