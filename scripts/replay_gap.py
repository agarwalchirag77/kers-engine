"""Replay scoring for messages the engine missed during an outage.

For every ticket, this compares the message_counts already present in
ers_events against the messages on disk and scores whatever is missing —
reconstructing the per-message trajectory the live engine would have produced
had it been up. Missing means missing anywhere, not just at the tail, so a
single failed evaluation in the middle of a conversation gets refilled on the
next run rather than staying invisible.

Three deliberate choices:

  * Each replayed row is stamped with the TIMESTAMP OF THE MESSAGE, not the
    moment we caught up, so `evaluated_at` stays chronologically honest and the
    trajectory can be plotted against real time. Commitment analysis also runs
    against that same historical moment, so DELIVERED/MISSED/PENDING reflects
    what was actually true then, not what is true now.

  * Only the FINAL score per ticket is pushed to Zendesk. The custom field holds
    one value; pushing 8 historical scores in sequence would churn the field and
    land on the same number anyway, while burning API calls.

  * A push happens only if this run scored the ticket's CURRENT head message.
    When only an interior hole was refilled, Zendesk already shows a score
    derived from a later message, and overwriting it would move the field
    backwards in time.

Usage:
  cd ~/kers-engine
  set -a; source .env; set +a
  .venv/bin/python scripts/replay_gap.py --dry-run     # scope + cost, no calls
  .venv/bin/python scripts/replay_gap.py               # do it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_client import OpenAIClient  # noqa: E402
from app.commitments import analyze_commitments  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_MODEL,
    PROMPT_VERSION,
    load_weight_config,
    load_zendesk_config,
)
from app.connectors.zendesk.pusher import ZendeskPusherHTTP  # noqa: E402
from app.ers import compute_ers  # noqa: E402
from app.models import ConversationContext, Message, TicketMetadata  # noqa: E402
from app.storage.db import Database  # noqa: E402

TICKET_DIR = ROOT / "ticket_files"
DB_PATH = ROOT / "data" / "escalation.sqlite"
CONCURRENCY = 4


def load_ticket(path: Path):
    raw = json.loads(path.read_text())
    msgs = [
        Message(
            author_type=m["author_type"],
            body=m["body"],
            timestamp=datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            if isinstance(m["timestamp"], str) else m["timestamp"],
        )
        for m in raw.get("messages", [])
    ]
    meta = TicketMetadata(
        ticket_id=str(raw.get("ticket_id") or path.stem),
        subject=raw.get("subject") or "",
        group_id=str(raw.get("group_id") or ""),
        priority=raw.get("priority") or "",
        channel=raw.get("channel") or "",
        tags=raw.get("tags") or [],
        locale=raw.get("locale") or "en-US",
        requester_id_hash=str(raw.get("requester_id_hash") or ""),
        agent_email=raw.get("agent_email") or "",
    )
    return raw, meta, msgs


def prefix_context(msgs, upto) -> ConversationContext:
    subset = msgs[:upto]
    lines = []
    for m in subset:
        who = "CUSTOMER" if m.author_type == "customer" else "AGENT"
        lines.append(f"[{m.timestamp.isoformat()}] {who}: {m.body}")
    return ConversationContext(
        formatted_messages="\n".join(lines),
        message_count=len(subset),
        messages=subset,
    )


def plan(min_msgs: int):
    """Which (ticket, [message_counts]) still need scoring.

    Works off the SET of already-scored message_counts, not just the maximum —
    so an interior hole (e.g. msg 13 failed while 14 and 15 succeeded) gets
    refilled on the next run instead of being invisible forever.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticket_id, message_count FROM ers_events"
    ).fetchall()
    conn.close()
    scored: dict[str, set] = {}
    for tid, n in rows:
        scored.setdefault(tid, set()).add(n)

    work = []
    for path in sorted(TICKET_DIR.glob("*.json")):
        tid = path.stem
        try:
            n_msgs = len(json.loads(path.read_text()).get("messages", []))
        except Exception:
            continue
        if n_msgs < min_msgs:
            continue
        have = scored.get(tid, set())
        todo = [n for n in range(min_msgs, n_msgs + 1) if n not in have]
        if todo:
            work.append((tid, path, todo, n_msgs))
    return work


async def replay_ticket(tid, path, todo, n_msgs, wc, ai, pusher, db, sem, stats):
    """Score the listed message_counts for one ticket, in order (each row's
    previous_ers/delta depends on the one before it).

    Pushes to Zendesk only when this run scored the ticket's CURRENT head
    (max(todo) == n_msgs). If we merely refilled an interior hole, the Zendesk
    field already holds a score from a later message and must not be
    overwritten with a stale one."""
    raw, meta, msgs = load_ticket(path)
    written = 0
    last = None

    for n in todo:
        async with sem:
            ctx = prefix_context(msgs, n)
            when = msgs[n - 1].timestamp          # historical moment
            try:
                analysis = analyze_commitments(
                    ctx.messages, evaluation_time=when, channel=meta.channel
                )
                sentiment = await ai.score(
                    ctx, meta, commitment_analysis=analysis, evaluation_time=when
                )
            except Exception as e:
                stats["errors"] += 1
                print(f"  {tid} @msg{n}: ERROR {type(e).__name__}: {str(e)[:70]}", flush=True)
                continue
            ers = compute_ers(sentiment.breakdown, wc.weights)
            last = (ers, sentiment, n, when)
            # intermediate rows record no push; the final one is pushed below
            from app.models import PushResult
            db.insert_ers_event(
                ticket_id=tid, ers=ers, sentiment=sentiment, message_count=n,
                threshold=wc.threshold,
                push_result=PushResult(success=False, error="replay: not pushed (intermediate)"),
                metadata=meta, model_version=ai.model, prompt_version=ai.prompt_version,
                evaluated_at=when,
            )
            written += 1
            stats["evals"] += 1

    # push only if we scored the ticket's current head
    if last is not None and last[2] == n_msgs:
        ers, sentiment, n, when = last
        push = await pusher.push_ers(tid, ers)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """UPDATE ers_events
               SET pushed_to_zd = ?, pushed_to_zd_at = ?, push_error = ?
               WHERE ticket_id = ? AND message_count = ? AND evaluated_at = ?""",
            (1 if push.success else 0,
             push.pushed_at.isoformat() if push.pushed_at else None,
             None if push.success else push.error,
             tid, n, when.isoformat()),
        )
        conn.commit()
        conn.close()
        if push.success:
            stats["pushed"] += 1
        else:
            stats["push_failed"] += 1
        print(f"  {tid}: +{written} evals, final ERS {ers:.2f} "
              f"({sentiment.top_signal}){'' if push.success else '  PUSH FAILED'}", flush=True)
    elif last is not None:
        print(f"  {tid}: +{written} evals (interior holes filled; not pushed — "
              f"head is msg {n_msgs})", flush=True)
        stats["holes_filled"] += written
    stats["tickets"] += 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wc = load_weight_config()
    work = plan(wc.min_messages_before_eval)
    total = sum(len(todo) for _, _, todo, _ in work)

    print(f"prompt:  {PROMPT_VERSION}")
    print(f"model:   {DEFAULT_MODEL}")
    print(f"tickets: {len(work)}   per-message evaluations: {total}   est ${total*0.02:.2f}")
    if args.dry_run:
        print("\ndry run — nothing scored. rerun without --dry-run.")
        return
    if not work:
        print("\nnothing to replay; already caught up.")
        return

    for var in ("OPENAI_API_KEY", "ZENDESK_API_TOKEN"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set", file=sys.stderr)
            sys.exit(1)

    zd = load_zendesk_config()
    db = Database(DB_PATH)
    ai = OpenAIClient(api_key=os.environ["OPENAI_API_KEY"], model=DEFAULT_MODEL)
    pusher = ZendeskPusherHTTP(
        subdomain=zd["subdomain"], api_user=zd["api_user"],
        api_token=os.environ[zd["api_token_env_var"]],
        custom_field_id=zd["ers_custom_field_id"],
    )
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"tickets": 0, "evals": 0, "pushed": 0, "push_failed": 0,
             "holes_filled": 0, "errors": 0}

    print("\nreplaying...\n", flush=True)
    # tickets run concurrently; messages within a ticket stay sequential
    await asyncio.gather(*[
        replay_ticket(tid, path, todo, n, wc, ai, pusher, db, sem, stats)
        for tid, path, todo, n in work
    ])
    await pusher.close()

    print()
    print(f"=== DONE  {stats} ===")


if __name__ == "__main__":
    asyncio.run(main())
