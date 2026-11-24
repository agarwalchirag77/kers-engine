#!/usr/bin/env python3
"""Read-only triage for "KERS stopped scoring".

Collapses the manual SSH triage in HANDOVER.md ("The four silent failures")
into one command that prints a single verdict. Touches nothing: it only reads
the SQLite DB and today's JSONL log, resolving their paths exactly the way
app/main.py does (ERS_DATA_DIR / ERS_LOG_DIR).

Run on the host with the environment loaded:

    set -a; . ./.env; set +a
    python3 scripts/diagnose.py

Note on scope: the logger deletes every prior-day engine-*.jsonl on the first
write of a new UTC day (app/storage/logger.py), so only *today's* log exists.
That is enough to characterise an ongoing failure; the DB's MAX(evaluated_at)
still gives the true moment scoring last succeeded across all history.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("ERS_DATA_DIR", "./data"))
LOG_DIR = Path(os.environ.get("ERS_LOG_DIR", "./logs"))
DB_PATH = DATA_DIR / "escalation.sqlite"

# Stages worth counting, in pipeline order. See app/worker.py + app/webhook.py.
STAGES = [
    "engine_started",
    "webhook_received",
    "event_skipped",
    "eval_skipped_filtered",
    "eval_skipped_under_threshold",
    "ai_called",
    "ai_response_invalid",
    "ai_response_received",
    "ers_computed",
    "pushed_to_zd",
    "push_failed",
    "db_written",
    "worker_exception",
    "missing_env",
]


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def read_db() -> dict:
    out: dict = {"exists": DB_PATH.exists(), "path": str(DB_PATH)}
    if not out["exists"]:
        return out
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out["total"] = conn.execute("SELECT COUNT(*) n FROM ers_events").fetchone()["n"]
        out["last_eval"] = conn.execute(
            "SELECT MAX(evaluated_at) m FROM ers_events"
        ).fetchone()["m"]
        out["by_version"] = [
            dict(r)
            for r in conn.execute(
                "SELECT prompt_version, COUNT(*) n, MAX(evaluated_at) last "
                "FROM ers_events GROUP BY prompt_version ORDER BY last DESC"
            ).fetchall()
        ]
        # Push health over the most recent 50 evaluations.
        recent = conn.execute(
            "SELECT pushed_to_zd, push_error FROM ers_events "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall()
        out["recent_n"] = len(recent)
        out["recent_push_fail"] = sum(1 for r in recent if not r["pushed_to_zd"])
        out["sample_push_error"] = next(
            (r["push_error"] for r in recent if not r["pushed_to_zd"] and r["push_error"]),
            None,
        )
    finally:
        conn.close()
    return out


def read_log() -> dict:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = LOG_DIR / f"engine-{date_str}.jsonl"
    out: dict = {"exists": path.exists(), "path": str(path)}
    if not out["exists"]:
        return out
    counts: Counter = Counter()
    last_webhook = None
    skip_reasons: Counter = Counter()
    last_ai_error = None
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stage = rec.get("stage", "")
            counts[stage] += 1
            if stage == "webhook_received":
                last_webhook = rec.get("timestamp")
            elif stage in ("event_skipped", "eval_skipped_filtered"):
                reason = (rec.get("extra") or {}).get("reason", "?")
                skip_reasons[reason] += 1
            elif stage == "ai_response_invalid":
                last_ai_error = rec.get("error")
    out["counts"] = {s: counts.get(s, 0) for s in STAGES}
    out["other_stages"] = {
        s: c for s, c in counts.items() if s not in STAGES
    }
    out["last_webhook"] = last_webhook
    out["skip_reasons"] = dict(skip_reasons.most_common(10))
    out["last_ai_error"] = last_ai_error
    return out


def verdict(db: dict, log: dict) -> list[str]:
    lines: list[str] = []
    c = log.get("counts", {}) if log.get("exists") else {}

    def cnt(s: str) -> int:
        return c.get(s, 0)

    if not log.get("exists"):
        lines.append(
            "No log for today yet. Either the engine hasn't written today, or "
            "ERS_LOG_DIR isn't the dir you think. Check the DB section above."
        )
    if cnt("missing_env"):
        lines.append(
            "missing_env logged -> OPENAI_API_KEY or ZENDESK_API_TOKEN not set "
            "in the engine's environment. Scores can't be computed or pushed."
        )

    if log.get("exists") and cnt("webhook_received") == 0:
        lines.append(
            "No webhook_received today -> events are NOT reaching the engine. "
            "If Zendesk shows 200s, you're pointed at a stale tunnel hostname "
            "(./deploy/update.sh --check-tunnel) OR it's just a quiet period."
        )
    elif cnt("webhook_received") > 0 and cnt("ai_called") == 0:
        lines.append(
            "Events arrive but none reach the model -> everything is being "
            "FILTERED/SKIPPED at the door. Top reasons: "
            + (json.dumps(log.get("skip_reasons", {})) or "{}")
            + ". Check filters.json group/tag scope vs. the tickets' real groups."
        )
    elif cnt("ai_called") > 0 and cnt("ai_response_received") == 0 and cnt("ai_response_invalid") > 0:
        lines.append(
            "Model calls all fail (ai_response_invalid, 0 received) -> most "
            "likely OpenAI 429 / balance exhausted, or a broken openai SDK "
            "version (unpinned requirements, open item #1). No DB rows get "
            "written in this state. Last error: " + str(log.get("last_ai_error"))
        )
    elif cnt("ers_computed") > 0 and cnt("push_failed") > 0 and cnt("pushed_to_zd") == 0:
        lines.append(
            "Scores compute but every push fails (push_failed) -> Zendesk push "
            "auth. The api_user is a named individual (khushi.s). Check that "
            "account is active and ZENDESK_API_TOKEN belongs to it, and that "
            "the ERS custom field still exists. DB rows ARE written with "
            "pushed_to_zd=0."
        )
    elif cnt("db_written") > 0:
        lines.append(
            "Pipeline completed db_written events today -> scoring is working "
            "now. If scores still look stale, suspect a specific ticket/group, "
            "or a gap earlier than today's log (check DB last_eval above)."
        )

    # DB-side corroboration, independent of today's log.
    if db.get("exists"):
        hrs = _hours_since(db.get("last_eval"))
        if hrs is not None and hrs > 24:
            lines.append(
                f"DB: last successful evaluation was {hrs:.1f}h ago "
                f"({db.get('last_eval')}). Scoring has been down at least that long."
            )
        if db.get("recent_push_fail"):
            lines.append(
                f"DB: {db['recent_push_fail']}/{db['recent_n']} most-recent rows "
                f"have pushed_to_zd=0 -> push failing. Sample: {db.get('sample_push_error')}"
            )
    if not lines:
        lines.append("No clear signal. Share this output and the DB/log sections.")
    return lines


def main() -> None:
    db = read_db()
    log = read_log()

    print("=" * 68)
    print("KERS diagnosis")
    print("=" * 68)

    print("\n[DB]", db["path"])
    if not db["exists"]:
        print("  MISSING — wrong ERS_DATA_DIR, or the engine never ran here.")
    else:
        print(f"  rows: {db['total']}   last eval: {db['last_eval']}")
        hrs = _hours_since(db.get("last_eval"))
        if hrs is not None:
            print(f"  last eval was {hrs:.1f}h ago")
        for r in db.get("by_version", []):
            print(f"    {r['prompt_version']}: {r['n']} rows, last {r['last']}")
        print(
            f"  recent push failures: {db.get('recent_push_fail')}/{db.get('recent_n')}"
        )

    print("\n[LOG]", log["path"])
    if not log["exists"]:
        print("  no log for today (UTC) yet")
    else:
        for s in STAGES:
            n = log["counts"].get(s, 0)
            if n:
                print(f"  {s:<32} {n}")
        if log.get("other_stages"):
            print("  other:", json.dumps(log["other_stages"]))
        if log.get("skip_reasons"):
            print("  skip reasons:", json.dumps(log["skip_reasons"]))

    print("\n[VERDICT]")
    for line in verdict(db, log):
        print("  -", line)
    print()


if __name__ == "__main__":
    main()
