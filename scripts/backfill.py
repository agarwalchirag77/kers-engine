"""One-shot backfill: score every ticket file that has accumulated
≥ min_messages_before_eval messages but hasn't been evaluated (or whose
latest evaluation predates the current message count).

Real-time scoring via /webhooks/zendesk/events only fires when a *new*
message arrives. If tickets sat in ticket_files/ before the engine was
running (or before the messaging adapter was added), they never get
evaluated through the live path. This script catches them up in one shot.

Usage:
    python -m scripts.backfill                # incremental: skip up-to-date
    python -m scripts.backfill --force        # re-score everything ≥ 6 msgs
    python -m scripts.backfill --ticket 71645 # just one ticket
    python -m scripts.backfill --dry-run      # show what WOULD be scored, no AI calls

Safe to run while uvicorn is up — SQLite serializes the two writers.
Does NOT push to Zendesk (push_error: "backfill: push disabled" on every row).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_client import AIClientError, OpenAIClient
from app.config import DEFAULT_MODEL, PROMPT_VERSION, load_weight_config
from app.ers import compute_ers
from app.models import PushResult
from app.storage.db import Database
from app.storage.logger import StructuredLogger
from app.storage.ticket_files import JsonTicketStore, TicketNotFoundError


DATA_DIR = Path(os.environ.get("ERS_DATA_DIR", "./data"))
TICKET_FILES_DIR = Path(os.environ.get("ERS_TICKET_DIR", "./ticket_files"))
LOG_DIR = Path(os.environ.get("ERS_LOG_DIR", "./logs"))
DB_PATH = DATA_DIR / "escalation.sqlite"


async def score_one(*, ticket_id, store, ai, db, logger, config, dry_run):
    """Score a single ticket. Returns (status, info) — status is one of:
    'scored', 'skipped_low', 'skipped_done', 'error'."""
    msg_count = store.get_message_count(ticket_id)
    if msg_count < config.min_messages_before_eval:
        return ("skipped_low", f"{msg_count} msgs")

    try:
        metadata = store.get_metadata(ticket_id)
        context = store.get_context_for_ai(ticket_id)
    except TicketNotFoundError as e:
        return ("error", f"store: {e}")

    if dry_run:
        return ("would_score", f"{msg_count} msgs, model={ai.model}")

    logger.info(
        stage="backfill_ai_called",
        ticket_id=ticket_id,
        extra={"message_count": context.message_count, "model": ai.model},
    )
    try:
        sentiment = await ai.score(context, metadata)
    except AIClientError as e:
        logger.error(stage="backfill_ai_failed", ticket_id=ticket_id, error=str(e)[:500])
        return ("error", f"ai: {e}")

    ers = compute_ers(sentiment.breakdown, config.weights)
    push_result = PushResult(success=False, error="backfill: push disabled")
    try:
        row_id = db.insert_ers_event(
            ticket_id=ticket_id,
            ers=ers,
            sentiment=sentiment,
            message_count=context.message_count,
            threshold=config.threshold,
            push_result=push_result,
            metadata=metadata,
            model_version=ai.model,
            prompt_version=ai.prompt_version,
        )
    except Exception as e:
        logger.error(stage="backfill_db_failed", ticket_id=ticket_id, error=str(e)[:500])
        return ("error", f"db: {e}")

    logger.info(
        stage="backfill_db_written",
        ticket_id=ticket_id,
        extra={"row_id": row_id, "ers": ers, "top_signal": sentiment.top_signal},
    )
    return ("scored", f"ERS={ers:.2f} top={sentiment.top_signal} conf={sentiment.confidence:.1f}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", help="Only this ticket id")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-score even if a DB row already exists at the current message count",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be scored without calling the AI",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        sys.exit("OPENAI_API_KEY not set (export it or pass --dry-run)")

    config = load_weight_config()
    ai = OpenAIClient(api_key=api_key or "dry-run") if not args.dry_run else _StubAI()
    store = JsonTicketStore(TICKET_FILES_DIR)
    db = Database(DB_PATH)
    logger = StructuredLogger(LOG_DIR)

    logger.info(stage="backfill_started", extra={"force": args.force, "dry_run": args.dry_run})

    # Discover candidates
    if args.ticket:
        ticket_ids = [args.ticket]
    else:
        ticket_ids = sorted(
            p.stem for p in TICKET_FILES_DIR.glob("*.json") if not p.stem.startswith("_")
        )

    print(f"found {len(ticket_ids)} ticket file(s); processing...")
    print(f"  min_messages_before_eval: {config.min_messages_before_eval}")
    print(f"  threshold:                {config.threshold}")
    print()

    summary = {"scored": 0, "skipped_low": 0, "skipped_done": 0, "would_score": 0, "error": 0}

    for tid in ticket_ids:
        msg_count = store.get_message_count(tid)

        # Idempotency: skip if a row already exists at the current count
        if not args.force:
            already = db.latest_evaluated_message_count(tid)
            if already and already >= msg_count and msg_count >= config.min_messages_before_eval:
                print(f"  {tid:8s}  msgs={msg_count:<4}  ALREADY @ {already}    (use --force to re-run)")
                summary["skipped_done"] += 1
                continue

        status, info = await score_one(
            ticket_id=tid, store=store, ai=ai, db=db, logger=logger, config=config,
            dry_run=args.dry_run,
        )
        marker = {
            "scored":       "  ✓",
            "would_score":  "  ?",
            "skipped_low":  "  -",
            "skipped_done": "  =",
            "error":        " !!",
        }.get(status, "  ?")
        print(f"{marker} {tid:8s}  msgs={msg_count:<4}  {status:<14}  {info}")
        summary[status] = summary.get(status, 0) + 1

    print()
    print("--- summary ---")
    for k, v in summary.items():
        if v:
            print(f"  {k:14s} {v}")

    logger.info(stage="backfill_finished", extra=summary)


class _StubAI:
    """Placeholder used when --dry-run is set so we don't need an API key."""
    model = "(dry-run)"
    prompt_version = "(dry-run)"
    async def score(self, *_a, **_kw):
        raise RuntimeError("dry-run stub should not be called")


if __name__ == "__main__":
    asyncio.run(main())
