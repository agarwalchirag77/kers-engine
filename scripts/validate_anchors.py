"""Calibration harness for the anchor tables embedded in the current prompt.

Source of truth: "Frustration Anchor Table Metrics.docx" — that document ships
20 labeled examples with the band each one SHOULD land in. This script feeds
each example through the real scoring path and reports whether the model puts
it in the documented band.

Two things are measured:
  ACCURACY    — did the score land in the band the doc says it should?
  CONSISTENCY — scoring the same example N times, how much does it move?

Every example is a single customer (or agent) message. Each is padded to the
minimum conversation length with neutral filler so the model sees a plausible
ticket, then scored. Only the metric under test is checked; the other seven
are ignored for that example.

Usage:
  cd ~/kers-engine
  set -a; source .env; set +a
  .venv/bin/python scripts/validate_anchors.py            # 3 runs each (default)
  .venv/bin/python scripts/validate_anchors.py --runs 1   # cheaper single pass

Cost: roughly $0.02 per call, so ~$0.40 for one pass of 20 examples,
~$1.20 for the default 3 runs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_client import OpenAIClient  # noqa: E402
from app.config import DEFAULT_MODEL, PROMPT_VERSION  # noqa: E402
from app.models import ConversationContext, Message, TicketMetadata  # noqa: E402


CONCURRENCY = 4

# Bands are half-open: [0,1) [1,2) [2,3) [3,4]
BANDS = {
    "[0,1)": (0.0, 1.0),
    "[1,2)": (1.0, 2.0),
    "[2,3)": (2.0, 3.0),
    "[3,4]": (3.0, 4.001),
}


def in_band(value: float, band: str) -> bool:
    lo, hi = BANDS[band]
    return lo <= value < hi


def band_of(value: float) -> str:
    for name, (lo, hi) in BANDS.items():
        if lo <= value < hi:
            return name
    return "OUT-OF-RANGE"


# ---------------------------------------------------------------------------
# The 20 labeled examples, verbatim from the anchor document.
# (metric, message, expected_band, author, why)
# ---------------------------------------------------------------------------

EXAMPLES: list[tuple[str, str, str, str, str]] = [
    # ---- frustration ----
    ("frustration",
     "Production ingestion has stopped. Please investigate.",
     "[0,1)", "customer",
     "High severity but factual; no expressed frustration."),
    ("frustration",
     "Production ingestion has been down for 6 hours and this is becoming frustrating.",
     "[2,3)", "customer",
     "Explicit frustration."),
    ("frustration",
     "This is completely unacceptable. We've reported this multiple times and still don't have a resolution.",
     "[3,4]", "customer",
     "Strong dissatisfaction and repeated unresolved issue."),

    # ---- urgency ----
    ("urgency",
     "Could you please check why yesterday's sync failed?",
     "[0,1)", "customer",
     "No immediate time pressure."),
    ("urgency",
     "Could you please prioritize this and let us know the ETA?",
     "[1,2)", "customer",
     "Preference for faster action, but no critical deadline."),
    ("urgency",
     "This is blocking our daily reporting. We need this resolved today.",
     "[2,3)", "customer",
     "Current operational impact and explicit deadline."),
    ("urgency",
     "Our production pipeline is down and business operations are currently blocked. We need this fixed immediately.",
     "[3,4]", "customer",
     "Critical ongoing operational impact."),

    # ---- confusion ----
    ("confusion",
     "Can you confirm whether this connector supports incremental sync?",
     "[0,1)", "customer",
     "Clear, specific question."),
    ("confusion",
     "I'm not sure why this field isn't appearing. Could you clarify?",
     "[1,2)", "customer",
     "Localized uncertainty."),
    ("confusion",
     "I'm confused. The dashboard says the sync succeeded, but the data isn't there. Which status should we trust?",
     "[2,3)", "customer",
     "Difficulty reconciling observed behavior."),
    ("confusion",
     "We don't understand what's happening anymore. We've followed the instructions, but we're seeing different behavior at every step.",
     "[3,4]", "customer",
     "Pervasive confusion preventing next action."),

    # ---- churn_threat ----
    ("churn_threat",
     "The sync is failing again. Can you investigate?",
     "[0,1)", "customer",
     "Complaint without retention signal."),
    ("churn_threat",
     "This is unacceptable. Please escalate this to your manager.",
     "[0,1)", "customer",
     "Escalation is NOT a churn threat."),
    ("churn_threat",
     "We're disappointed that we're still facing these issues.",
     "[1,2)", "customer",
     "Dissatisfaction but no departure intent."),
    ("churn_threat",
     "If these reliability issues continue, we'll have to start evaluating alternatives.",
     "[2,3)", "customer",
     "Explicit consideration of alternatives."),
    ("churn_threat",
     "We've decided to move this workload to another provider if this isn't resolved.",
     "[3,4]", "customer",
     "Explicit vendor replacement intent."),

    # ---- agent_tone ----
    ("agent_tone",
     "Thanks for sharing the details. I've reviewed the logs and found the cause. Here's what we recommend.",
     "[0,1)", "agent",
     "Professional, clear, and proactive."),
    ("agent_tone",
     "I've checked this and the next step is to restart the sync. Please let us know once that's done.",
     "[1,2)", "agent",
     "Professional but transactional."),
    ("agent_tone",
     "As I mentioned earlier, this is expected behavior. Please review the documentation.",
     "[2,3)", "agent",
     "Noticeably curt / dismissive."),
    ("agent_tone",
     "You clearly haven't followed the instructions correctly.",
     "[3,4]", "agent",
     "Blaming and condescending."),
]


# Neutral filler so the conversation reaches a plausible length without
# introducing signal that could contaminate the metric under test.
FILLER = [
    ("customer", "Hi team, opening a ticket about pipeline #12."),
    ("agent", "Hello, thanks for reaching out. Could you share the pipeline number and object name?"),
    ("customer", "Pipeline number is 12, object is orders."),
    ("agent", "Thank you, we have the details and are taking a look."),
    ("customer", "Noted, thanks."),
]


def build_context(message: str, author: str) -> tuple[ConversationContext, TicketMetadata]:
    """Neutral filler conversation with the example as the final message."""
    base = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    msgs: list[Message] = []
    for i, (who, body) in enumerate(FILLER):
        msgs.append(Message(author_type=who, body=body, timestamp=base + timedelta(minutes=i * 3)))
    msgs.append(
        Message(
            author_type=author,
            body=message,
            timestamp=base + timedelta(minutes=len(FILLER) * 3),
        )
    )

    lines = []
    for m in msgs:
        who = "CUSTOMER" if m.author_type == "customer" else "AGENT"
        lines.append(f"[{m.timestamp.isoformat()}] {who}: {m.body}")

    context = ConversationContext(
        formatted_messages="\n".join(lines),
        message_count=len(msgs),
        messages=msgs,
    )
    metadata = TicketMetadata(
        ticket_id="anchor-validation",
        subject="Anchor calibration example",
        channel="email",
        priority="normal",
        locale="en-US",
    )
    return context, metadata


async def score_example(client, idx, metric, message, expected, author, why, runs, sem):
    context, metadata = build_context(message, author)
    values: list[float] = []
    errors: list[str] = []
    for _ in range(runs):
        async with sem:
            try:
                s = await client.score(context, metadata)
                values.append(float(getattr(s.breakdown, metric)))
            except Exception as e:
                errors.append(f"{type(e).__name__}: {str(e)[:80]}")
    return {
        "idx": idx, "metric": metric, "message": message, "expected": expected,
        "why": why, "values": values, "errors": errors,
    }


def _clip(s: str, w: int) -> str:
    s = " ".join(s.split())
    return (s[: w - 1] + "…") if len(s) > w else s


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3,
                    help="times to score each example (default 3, for consistency measurement)")
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    n_calls = len(EXAMPLES) * args.runs
    print(f"prompt:      {PROMPT_VERSION}")
    print(f"model:       {DEFAULT_MODEL}")
    print(f"examples:    {len(EXAMPLES)}   runs each: {args.runs}   total calls: {n_calls}")
    print(f"est. cost:   ~${n_calls * 0.02:.2f}")
    print("running...", flush=True)

    client = OpenAIClient(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        score_example(client, i, m, msg, exp, who, why, args.runs, sem)
        for i, (m, msg, exp, who, why) in enumerate(EXAMPLES, start=1)
    ]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r["idx"])

    # ---- per-example table ----
    print()
    print("=" * 132)
    print(f"{'#':>3}  {'metric':<28}  {'want':<7}  {'got':<7}  {'values':<18}  {'spread':>6}  {'hit':<4}  message")
    print("-" * 132)

    hits = 0
    scored = 0
    spreads: list[float] = []
    failures: list[dict] = []

    for r in results:
        if not r["values"]:
            print(f"{r['idx']:>3}  {r['metric']:<28}  {r['expected']:<7}  {'ERR':<7}  {'-':<18}  {'-':>6}  {'-':<4}  {_clip(r['message'], 40)}")
            for e in r["errors"]:
                print(f"       error: {e}")
            continue

        scored += 1
        median = statistics.median(r["values"])
        got = band_of(median)
        spread = max(r["values"]) - min(r["values"])
        spreads.append(spread)
        hit = got == r["expected"]
        if hit:
            hits += 1
        else:
            failures.append({**r, "median": median, "got": got})

        vals = ",".join(f"{v:.1f}" for v in r["values"])
        print(f"{r['idx']:>3}  {r['metric']:<28}  {r['expected']:<7}  {got:<7}  {vals:<18}  {spread:>6.2f}  {'YES' if hit else 'no':<4}  {_clip(r['message'], 40)}")

    # ---- summary ----
    print()
    print("=" * 132)
    if scored:
        print(f"ACCURACY     {hits}/{scored} examples landed in the documented band  ({hits / scored * 100:.0f}%)")
    if spreads:
        exact = sum(1 for s in spreads if s == 0.0)
        print(f"CONSISTENCY  {exact}/{len(spreads)} examples identical across {args.runs} runs; "
              f"mean spread {statistics.mean(spreads):.3f}, max spread {max(spreads):.2f}")

    # ---- per-metric breakdown ----
    print()
    print("per-metric accuracy:")
    by_metric: dict[str, list[bool]] = {}
    for r in results:
        if not r["values"]:
            continue
        got = band_of(statistics.median(r["values"]))
        by_metric.setdefault(r["metric"], []).append(got == r["expected"])
    for metric, flags in by_metric.items():
        print(f"  {metric:<28}  {sum(flags)}/{len(flags)}")

    # ---- the two gate examples from the plan ----
    print()
    print("critical guardrail checks (severity/escalation must NOT inflate):")
    for r in results:
        if not r["values"]:
            continue
        msg = r["message"]
        if msg.startswith("Production ingestion has stopped"):
            median = statistics.median(r["values"])
            ok = in_band(median, "[0,1)")
            print(f"  factual severity -> frustration {median:.1f} ({band_of(median)})  "
                  f"{'PASS' if ok else 'FAIL — severity is still inflating frustration'}")
        if msg.startswith("This is unacceptable. Please escalate"):
            median = statistics.median(r["values"])
            ok = in_band(median, "[0,1)")
            print(f"  escalation -> churn_threat      {median:.1f} ({band_of(median)})  "
                  f"{'PASS' if ok else 'FAIL — escalation is still read as churn'}")

    # ---- misses in detail ----
    if failures:
        print()
        print("misses in detail:")
        for f in failures:
            print(f"  #{f['idx']} {f['metric']}: wanted {f['expected']}, got {f['got']} (median {f['median']:.1f})")
            print(f"      message:  {_clip(f['message'], 110)}")
            print(f"      doc says: {f['why']}")

    print()
    return 0 if scored and hits == scored else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
