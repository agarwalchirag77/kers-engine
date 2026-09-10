"""Reclassify author_type on existing ticket_files using Zendesk ROLE.

Ticket files rebuilt after an outage were classified by "is this the ticket
requester?", which labels every CC'd colleague, second engineer, or
shared-inbox reply on the customer side as Hevo staff. Measured at 12.6% of
messages across 17% of tickets.

This rewrites author_type from `data/agent_directory.json`, where the role
came from Zendesk itself. Only messages whose author has a known role are
touched; synthetic authors (e.g. id -1 for system-generated chat transcripts)
are left alone and reported separately.

The live webhook path was never affected - the adapter uses Zendesk's own
`is_staff` flag for comments and `actor.type` for messaging, both
authoritative.

Usage:
  cd ~/kers-engine
  .venv/bin/python scripts/repair_author_types.py --dry-run
  .venv/bin/python scripts/repair_author_types.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKET_DIR = ROOT / "ticket_files"
DIRECTORY = ROOT / "data" / "agent_directory.json"
STAFF_ROLES = ("agent", "admin")
STAFF_EMAIL_DOMAIN = "@hevodata.com"


def is_staff(entry: dict) -> bool:
    """Staff iff a Hevo email OR a staff role.

    Neither signal alone is sufficient. Measured across the 129-user
    directory:
      - 92 external users, all role=end-user            (role agrees)
      - 27 hevodata.com users, role agent/admin         (role agrees)
      -  6 staff-domain users, role=end-user            (role WRONG - these
           are serving agents whose user records are mis-set)
      -  3 "Permanently deleted user", role=agent, no email (email missing)

    The disagreement is one-directional: no external address is ever marked
    staff. So the union of both signals is safe - it recovers the six
    mis-set records and the three deleted ex-staff, and cannot promote a
    customer to staff.
    """
    email = (entry.get("email") or "").lower()
    if email.endswith(STAFF_EMAIL_DOMAIN):
        return True
    return (entry.get("role") or "") in STAFF_ROLES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DIRECTORY.exists():
        print("ERROR: %s missing - run scripts/backfill_author_ids.py first" % DIRECTORY,
              file=sys.stderr)
        sys.exit(1)
    directory = json.loads(DIRECTORY.read_text())

    stats = {"tickets_scanned": 0, "tickets_changed": 0, "msgs_changed": 0,
             "no_role": 0, "no_author_id": 0, "unchanged": 0}
    changed_tickets = []
    examples = []

    for path in sorted(TICKET_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        msgs = data.get("messages") or []
        if not msgs:
            continue
        stats["tickets_scanned"] += 1
        n_changed = 0

        for m in msgs:
            aid = str(m.get("author_id") or "")
            if not aid:
                stats["no_author_id"] += 1
                continue
            entry = directory.get(aid)
            if not entry or (not entry.get("email") and not entry.get("role")):
                # nothing to judge on (e.g. synthetic author id -1 used for
                # system-generated chat transcripts) - leave it alone
                stats["no_role"] += 1
                continue
            correct = "agent" if is_staff(entry) else "customer"
            if m.get("author_type") != correct:
                if len(examples) < 10:
                    examples.append((
                        path.stem, m.get("author_type"), correct,
                        (directory.get(aid) or {}).get("name", "?"),
                        (entry.get("email") or entry.get("role") or "?"),
                        " ".join((m.get("body") or "").split())[:52],
                    ))
                m["author_type"] = correct
                n_changed += 1
                stats["msgs_changed"] += 1
            else:
                stats["unchanged"] += 1

        if n_changed:
            stats["tickets_changed"] += 1
            changed_tickets.append((path.stem, n_changed))
            if not args.dry_run:
                path.write_text(json.dumps(data, indent=2))

    print("mode: %s" % ("DRY RUN - nothing written" if args.dry_run else "LIVE"))
    print()
    print("=== %s ===" % stats)
    print()
    print("=== examples of corrections ===")
    for tid, was, now, name, role, body in examples:
        print("  ticket %-7s %s -> %-8s  %-34s role=%-9s %s"
              % (tid, was, now, name[:32], role, body))
    print()
    print("=== tickets needing a re-score (author mix changed) : %d ===" % len(changed_tickets))
    for tid, n in sorted(changed_tickets, key=lambda x: -x[1])[:25]:
        print("  %-8s %d message(s) reclassified" % (tid, n))

    if changed_tickets and not args.dry_run:
        out = ROOT / "data" / "rescore_needed.json"
        out.write_text(json.dumps([t for t, _ in changed_tickets], indent=2))
        print()
        print("wrote %s - feed this to the re-score step" % out)


if __name__ == "__main__":
    main()
