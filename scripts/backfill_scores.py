"""One-shot backfill: score every existing ticket_files/*.json under the
current prompt (v4) and push to Zendesk. Runs the same evaluation flow the
live engine uses — just skips the debounce/queue path.

Use after a fresh deploy where the DB is empty but ticket_files/ are hydrated
(e.g., after `restore_ticket_files.py`). Idempotency: rows are stamped with
evaluated_at now; running twice will just create two rows per ticket.

Environment: reads OPENAI_API_KEY, ZENDESK_API_TOKEN from `.env` (already
sourced by the shell) or from OS env.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # ~/kers-engine
sys.path.insert(0, str(ROOT))

from app.ai_client import OpenAIClient  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_MODEL,
    PROMPT_VERSION,
    load_weight_config,
    load_zendesk_config,
)
from app.connectors.zendesk.pusher import ZendeskPusherHTTP  # noqa: E402
from app.ers import compute_ers  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.ticket_files import JsonTicketStore  # noqa: E402


CONCURRENCY = 4
TICKET_FILES_DIR = ROOT / "ticket_files"
DB_PATH = ROOT / "data" / "escalation.sqlite"


async def score_one(
    ticket_id: str,
    weight_config,
    ai_client: OpenAIClient,
    ticket_store: JsonTicketStore,
    zd_pusher: ZendeskPusherHTTP,
    db: Database,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        result = {"ticket_id": ticket_id, "status": "?", "ers": None, "detail": ""}
        try:
            msg_count = ticket_store.get_message_count(ticket_id)
            if msg_count < weight_config.min_messages_before_eval:
                result["status"] = "skipped_under_threshold"
                result["detail"] = f"{msg_count} < {weight_config.min_messages_before_eval}"
                return result

            metadata = ticket_store.get_metadata(ticket_id)
            context = ticket_store.get_context_for_ai(ticket_id)
            sentiment = await ai_client.score(context, metadata)
            ers = compute_ers(sentiment.breakdown, weight_config.weights)

            push_result = await zd_pusher.push_ers(ticket_id, ers)
            db.insert_ers_event(
                ticket_id=ticket_id,
                ers=ers,
                sentiment=sentiment,
                message_count=msg_count,
                threshold=weight_config.threshold,
                push_result=push_result,
                metadata=metadata,
                model_version=ai_client.model,
                prompt_version=ai_client.prompt_version,
            )
            result["status"] = "scored" if push_result.success else "scored_push_failed"
            result["ers"] = ers
            result["detail"] = sentiment.top_signal
            return result
        except Exception as e:
            result["status"] = "error"
            result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
            return result


async def main():
    for env_var in ("OPENAI_API_KEY", "ZENDESK_API_TOKEN"):
        if not os.environ.get(env_var):
            print(f"ERROR: {env_var} not set", file=sys.stderr)
            sys.exit(1)

    weight_config = load_weight_config()
    zd_config = load_zendesk_config()
    ticket_store = JsonTicketStore(TICKET_FILES_DIR)
    db = Database(DB_PATH)
    ai_client = OpenAIClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=DEFAULT_MODEL,
    )
    zd_pusher = ZendeskPusherHTTP(
        subdomain=zd_config["subdomain"],
        api_user=zd_config["api_user"],
        api_token=os.environ[zd_config["api_token_env_var"]],
        custom_field_id=zd_config["ers_custom_field_id"],
    )

    # Enumerate ticket_files on disk (skip our backup dir if present)
    ticket_ids = sorted(
        p.stem for p in TICKET_FILES_DIR.glob("*.json")
        if not p.name.startswith(".")
    )

    # Optional flag: only score tickets that have no row in ers_events yet.
    # Prevents re-scoring (and re-paying) for tickets already in the DB.
    if "--only-unscored" in sys.argv:
        with sqlite3.connect(DB_PATH) as conn:
            already = {r[0] for r in conn.execute("SELECT DISTINCT ticket_id FROM ers_events")}
        pre = len(ticket_ids)
        ticket_ids = [t for t in ticket_ids if t not in already]
        print(f"--only-unscored: skipping {pre - len(ticket_ids)} already-scored tickets")

    total = len(ticket_ids)

    print(f"prompt version:     {PROMPT_VERSION}")
    print(f"model:              {DEFAULT_MODEL}")
    print(f"tickets on disk:    {total}")
    print(f"min_messages:       {weight_config.min_messages_before_eval}")
    print(f"concurrency:        {CONCURRENCY}")
    print("running...")

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        score_one(tid, weight_config, ai_client, ticket_store, zd_pusher, db, sem)
        for tid in ticket_ids
    ]

    scored = skipped = errors = pushed_ok = pushed_fail = 0
    threshold_breaches = []
    t0 = time.monotonic()
    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        r = await coro
        if r["status"] == "scored":
            scored += 1
            pushed_ok += 1
            if r["ers"] is not None and r["ers"] >= weight_config.threshold:
                threshold_breaches.append((r["ticket_id"], r["ers"], r["detail"]))
        elif r["status"] == "scored_push_failed":
            scored += 1
            pushed_fail += 1
        elif r["status"] == "skipped_under_threshold":
            skipped += 1
        else:
            errors += 1
            print(f"  [{i}/{total}] {r['ticket_id']} ERROR: {r['detail']}", flush=True)
        if i % 10 == 0 or i == total:
            print(f"  [{i}/{total}] scored={scored} skipped={skipped} errors={errors}", flush=True)

    dt = time.monotonic() - t0
    print()
    print(f"=== DONE in {dt:.1f}s ===")
    print(f"  scored:                {scored}")
    print(f"  skipped (< min msgs):  {skipped}")
    print(f"  errors:                {errors}")
    print(f"  pushed to Zendesk ok:  {pushed_ok}")
    print(f"  push failed:           {pushed_fail}")
    print()
    if threshold_breaches:
        print(f"  ⚠ tickets at/above ers>={weight_config.threshold} threshold ({len(threshold_breaches)}):")
        for tid, ers, top in sorted(threshold_breaches, key=lambda x: -x[1]):
            print(f"     {tid}  ers={ers:.2f}  top={top}")
    else:
        print(f"  no tickets crossed the {weight_config.threshold} threshold")


if __name__ == "__main__":
    asyncio.run(main())
