"""Interactive ERS simulator.

Exercises the real OpenAIClient + ERS calculator + (optionally) the SQLite DB
and JSONL logger, without needing Zendesk webhooks, the ERS custom field,
the worker, or the debounce.

Usage:
    # interactive mode — type messages, watch ERS evolve
    python -m scripts.simulate

    # replay a JSON conversation
    python -m scripts.simulate --replay scripts/sample_conversations/escalating.json

    # see the exact prompt sent to OpenAI on each eval
    python -m scripts.simulate --show-prompt

    # skip DB and JSONL writes (default: persist)
    python -m scripts.simulate --no-persist --replay scripts/sample_conversations/calm.json

Requires `OPENAI_API_KEY` in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_client import OpenAIClient, AIClientError
from app.config import DEFAULT_MODEL, load_weight_config
from app.ers import compute_ers
from app.models import Message, PushResult, TicketMetadata, WeightConfig
from app.storage.db import Database
from app.storage.logger import StructuredLogger
from app.storage.ticket_files import JsonTicketStore


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def color_for_ers(ers: float, threshold: float) -> str:
    if ers >= threshold:
        return RED
    if ers >= threshold * 0.75:
        return YELLOW
    return GREEN


def print_result(ers: float, prev_ers: float | None, sentiment, threshold: float) -> None:
    delta = (ers - prev_ers) if prev_ers is not None else None
    delta_str = f"  (Δ {delta:+.2f})" if delta is not None else ""
    breach = f"  {RED}{BOLD}[BREACHED]{RESET}" if ers >= threshold else ""
    color = color_for_ers(ers, threshold)

    print()
    print(f"  {color}{BOLD}ERS: {ers:.2f}{RESET}{delta_str}{breach}  "
          f"{DIM}threshold: {threshold:.2f}{RESET}")
    print(f"  top_signal: {BOLD}{sentiment.top_signal}{RESET}    "
          f"confidence: {sentiment.confidence:.2f}/5.00")
    print(f"  reasoning: {sentiment.reasoning}")
    if sentiment.commitments_detected > 0:
        print(f"  commitments: {sentiment.commitments_missed}/"
              f"{sentiment.commitments_detected} missed")
    print(f"  {DIM}breakdown:{RESET}")
    for name, val in sentiment.breakdown.as_dict().items():
        bar = "█" * int(val * 4) + "░" * (20 - int(val * 4))
        print(f"    {name:30s} {val:.2f}  {DIM}{bar}{RESET}")
    print()


async def run_eval(
    store,
    ai,
    config,
    ticket_id,
    metadata,
    show_prompt,
    db: Database | None = None,
    logger: StructuredLogger | None = None,
):
    """Evaluate if we have enough messages; return (ers, sentiment) or None.

    When `db` is provided, persists a row to ers_events. When `logger` is
    provided, emits structured JSONL events at each stage (matching what the
    real Worker would emit in production)."""
    count = store.get_message_count(ticket_id)
    if count < config.min_messages_before_eval:
        remaining = config.min_messages_before_eval - count
        print(f"  {DIM}{count}/{config.min_messages_before_eval} messages — "
              f"{remaining} more before first eval{RESET}")
        if logger:
            logger.info(
                stage="eval_skipped_under_threshold",
                ticket_id=ticket_id,
                extra={"message_count": count, "min": config.min_messages_before_eval},
            )
        return None

    context = store.get_context_for_ai(ticket_id)
    if show_prompt:
        print(f"\n{DIM}--- PROMPT ---{RESET}")
        print(ai._build_prompt(context, metadata))
        print(f"{DIM}--- END PROMPT ---{RESET}\n")

    if logger:
        logger.info(
            stage="ai_called",
            ticket_id=ticket_id,
            extra={"message_count": count, "model": ai.model},
        )
    print(f"  {DIM}calling OpenAI ({ai.model}, {count} msgs)...{RESET}",
          end="", flush=True)
    start = _time.monotonic()
    try:
        sentiment = await ai.score(context, metadata)
    except AIClientError as e:
        print(f"\n  {RED}AI call failed: {e}{RESET}")
        if logger:
            logger.error(
                stage="ai_response_invalid",
                ticket_id=ticket_id,
                error=str(e),
                duration_ms=(_time.monotonic() - start) * 1000,
            )
        return None
    duration_ms = (_time.monotonic() - start) * 1000
    print("\r" + " " * 80 + "\r", end="")  # clear the "calling..." line
    if logger:
        logger.info(
            stage="ai_response_received",
            ticket_id=ticket_id,
            duration_ms=duration_ms,
            extra={
                "top_signal": sentiment.top_signal,
                "confidence": sentiment.confidence,
            },
        )

    ers = compute_ers(sentiment.breakdown, config.weights)
    if logger:
        logger.info(
            stage="ers_computed",
            ticket_id=ticket_id,
            extra={"ers": ers, "threshold": config.threshold},
        )

    # Persist to DB if enabled. We pass a "push disabled" PushResult since the
    # simulator never actually PUTs to Zendesk — the row is honest about that.
    if db is not None:
        push_result = PushResult(success=False, error="simulator: push disabled")
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
            if logger:
                logger.info(
                    stage="db_written", ticket_id=ticket_id, extra={"row_id": row_id}
                )
        except Exception as e:
            print(f"  {RED}DB write failed: {e}{RESET}")
            if logger:
                logger.error(
                    stage="db_write_failed", ticket_id=ticket_id, error=str(e)
                )

    return ers, sentiment


def parse_line(line: str):
    """'c hello' -> ('customer', 'hello'); 'a hi' -> ('agent', 'hi')."""
    parts = line.strip().split(" ", 1)
    if len(parts) < 2:
        return None
    role, body = parts[0].lower(), parts[1]
    if role in ("c", "customer"):
        return "customer", body
    if role in ("a", "agent"):
        return "agent", body
    return None


async def interactive(
    args,
    store,
    ai,
    config: WeightConfig,
    metadata,
    db: Database | None = None,
    logger: StructuredLogger | None = None,
):
    print(f"{BOLD}ERS Engine Simulator{RESET} {DIM}(interactive){RESET}")
    print(f"{DIM}=================================={RESET}")
    print(f"Threshold: {config.threshold:.2f}    Min messages: {config.min_messages_before_eval}    Model: {ai.model}")
    print(f"Ticket ID: {args.ticket_id}")
    print()
    print("Commands:")
    print(f"  {BOLD}c <body>{RESET}      add a customer message")
    print(f"  {BOLD}a <body>{RESET}      add an agent message")
    print(f"  {BOLD}show{RESET}          print conversation so far")
    print(f"  {BOLD}reset{RESET}         clear and start fresh")
    print(f"  {BOLD}quit{RESET}          exit (or Ctrl-D)")
    print()

    prev_ers: float | None = None
    msg_index = 0

    while True:
        try:
            line = input(f"[{msg_index + 1}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        if line == "show":
            if store.get_message_count(args.ticket_id) > 0:
                ctx = store.get_context_for_ai(args.ticket_id)
                print(ctx.formatted_messages)
            else:
                print(f"  {DIM}(empty){RESET}")
            continue
        if line == "reset":
            store.purge(args.ticket_id)
            store.get_or_create(args.ticket_id, metadata)
            prev_ers = None
            msg_index = 0
            print(f"  {DIM}conversation reset{RESET}")
            continue

        parsed = parse_line(line)
        if parsed is None:
            print(f"  {YELLOW}format: c <body> | a <body> | show | reset | quit{RESET}")
            continue
        author, body = parsed
        msg = Message(
            timestamp=datetime.now(timezone.utc),
            author_type=author,
            body=body,
        )
        store.append_message(args.ticket_id, msg)
        msg_index += 1

        result = await run_eval(
            store, ai, config, args.ticket_id, metadata,
            args.show_prompt, db=db, logger=logger,
        )
        if result is not None:
            ers, sentiment = result
            print_result(ers, prev_ers, sentiment, config.threshold)
            prev_ers = ers


async def replay(
    args,
    store,
    ai,
    config: WeightConfig,
    metadata,
    db: Database | None = None,
    logger: StructuredLogger | None = None,
):
    """Replay a conversation from a JSON file."""
    path = Path(args.replay)
    if not path.exists():
        sys.exit(f"replay file not found: {path}")

    with path.open() as f:
        messages = json.load(f)
    if not isinstance(messages, list):
        sys.exit("replay file must be a JSON array of {author, body, timestamp?}")

    print(f"{BOLD}Replaying{RESET} {path.name} ({len(messages)} messages)")
    print(f"{DIM}=================================={RESET}")
    print(f"Threshold: {config.threshold:.2f}    Min messages: {config.min_messages_before_eval}    Model: {ai.model}")
    print(f"Ticket ID: {args.ticket_id}\n")

    prev_ers: float | None = None
    synthetic_base = datetime.now(timezone.utc)

    for i, m in enumerate(messages):
        author = m["author"]
        body = m["body"]
        ts_raw = m.get("timestamp")
        if ts_raw:
            timestamp = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            # Space synthetic timestamps 5 minutes apart so the AI can reason
            # about commitment windows even when the fixture omits them.
            timestamp = synthetic_base + timedelta(minutes=5 * i)

        msg = Message(timestamp=timestamp, author_type=author, body=body)
        store.append_message(args.ticket_id, msg)

        color = "\033[36m" if author == "customer" else "\033[35m"
        print(f"[{i + 1}] {color}{author}{RESET} "
              f"{DIM}({timestamp.strftime('%H:%M')}){RESET}  {body}")

        result = await run_eval(
            store, ai, config, args.ticket_id, metadata,
            args.show_prompt, db=db, logger=logger,
        )
        if result is not None:
            ers, sentiment = result
            print_result(ers, prev_ers, sentiment, config.threshold)
            prev_ers = ers


def _resolve_ticket_id(args) -> str:
    """If the user didn't pass --ticket-id, generate one with a timestamp so
    each run lands as its own ticket in the DB (clean separation)."""
    if args.ticket_id:
        return args.ticket_id
    suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.replay:
        base = Path(args.replay).stem  # e.g. 'calm'
        return f"sim-{base}-{suffix}"
    return f"sim-interactive-{suffix}"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replay", help="JSON file of [{author, body, timestamp?}] to replay"
    )
    parser.add_argument(
        "--ticket-id",
        default=None,
        help=("Ticket ID for DB rows (default: sim-<replay-name>-<timestamp> "
              "or sim-interactive-<timestamp>)"),
    )
    parser.add_argument("--subject", default="Simulator session")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the full prompt sent to OpenAI before each eval",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip DB and JSONL writes (default: persist to data/escalation.sqlite and logs/)",
    )
    parser.add_argument(
        "--db-path",
        default="data/escalation.sqlite",
        help="SQLite DB path (relative to engine root or absolute)",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="JSONL log directory (relative to engine root or absolute)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set. `export OPENAI_API_KEY=sk-...`")

    args.ticket_id = _resolve_ticket_id(args)

    config = load_weight_config()
    ai = OpenAIClient(
        api_key=api_key,
        model=os.environ.get("ERS_MODEL", DEFAULT_MODEL),
    )

    # Resolve persistence targets. Relative paths resolve against the engine
    # root (the parent of scripts/), so running the simulator from anywhere
    # still writes to the same DB and log dir.
    db: Database | None = None
    logger: StructuredLogger | None = None
    if not args.no_persist:
        db_path = Path(args.db_path)
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        log_dir = Path(args.log_dir)
        if not log_dir.is_absolute():
            log_dir = ROOT / log_dir
        db = Database(db_path)
        logger = StructuredLogger(log_dir)
        print(f"{DIM}persisting → db: {db_path}{RESET}")
        print(f"{DIM}persisting → logs: {log_dir}{RESET}\n")
        logger.info(
            stage="simulator_started",
            ticket_id=args.ticket_id,
            extra={
                "mode": "replay" if args.replay else "interactive",
                "replay_file": args.replay,
                "model": ai.model,
                "threshold": config.threshold,
            },
        )

    with tempfile.TemporaryDirectory(prefix="ers-sim-") as tmp:
        store = JsonTicketStore(Path(tmp))
        metadata = TicketMetadata(
            ticket_id=args.ticket_id,
            subject=args.subject,
            channel="email",
            locale="en-US",
        )
        store.get_or_create(args.ticket_id, metadata)

        try:
            if args.replay:
                await replay(args, store, ai, config, metadata, db=db, logger=logger)
            else:
                await interactive(args, store, ai, config, metadata, db=db, logger=logger)
        finally:
            if logger:
                logger.info(stage="simulator_finished", ticket_id=args.ticket_id)


if __name__ == "__main__":
    asyncio.run(main())
