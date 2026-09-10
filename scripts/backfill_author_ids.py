"""Backfill `author_id` onto messages in existing ticket_files, and build a
local id -> name map for the agents involved.

Existing ticket files record only `author_type: customer|agent`, so an ERS
movement cannot be attributed to a specific agent. Zendesk keeps `author_id`
on every comment indefinitely, so this is recoverable at any time.

Matching is by (author_type, timestamp) against the comments Zendesk returns,
falling back to position when timestamps do not line up exactly. Messages that
cannot be matched are left untouched rather than guessed at.

Writes:
  ticket_files/*.json            author_id added per message
  data/agent_directory.json      {zendesk_user_id: {name, email, role}}

Usage:
  cd ~/kers-engine
  set -a; source .env; set +a
  .venv/bin/python scripts/backfill_author_ids.py --dry-run
  .venv/bin/python scripts/backfill_author_ids.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TICKET_DIR = ROOT / "ticket_files"
DIRECTORY = ROOT / "data" / "agent_directory.json"
ZD = json.loads((ROOT / "app" / "connectors" / "zendesk" / "settings.json").read_text())


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get(ZD["api_token_env_var"])
    if not token:
        print("ERROR: %s not set" % ZD["api_token_env_var"], file=sys.stderr)
        sys.exit(1)
    auth = (ZD["api_user"] + "/token", token)
    base = "https://" + ZD["subdomain"] + ".zendesk.com/api/v2"

    files = sorted(TICKET_DIR.glob("*.json"))
    print("mode: %s" % ("DRY RUN" if args.dry_run else "LIVE"))
    print("ticket files: %d" % len(files))
    print()

    directory = json.loads(DIRECTORY.read_text()) if DIRECTORY.exists() else {}
    stats = {"tickets": 0, "msgs_matched": 0, "msgs_unmatched": 0,
             "already_had": 0, "fetch_failed": 0}
    seen_ids = set()

    for path in files:
        tid = path.stem
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        msgs = data.get("messages") or []
        if not msgs:
            continue
        if all(m.get("author_id") for m in msgs):
            stats["already_had"] += 1
            for m in msgs:
                seen_ids.add(str(m["author_id"]))
            continue

        try:
            r = httpx.get(base + "/tickets/" + tid + "/comments.json", auth=auth, timeout=30)
            if r.status_code != 200:
                stats["fetch_failed"] += 1
                continue
            comments = r.json().get("comments", [])
            t = httpx.get(base + "/tickets/" + tid + ".json", auth=auth, timeout=30)
            requester = t.json()["ticket"].get("requester_id") if t.status_code == 200 else None
        except Exception:
            stats["fetch_failed"] += 1
            continue

        # public comments only, in order - mirrors how the file was built
        pub = [c for c in comments if c.get("public") and (c.get("body") or "").strip()]

        # index by (author_type, timestamp) for exact matching
        by_key = {}
        for c in pub:
            atype = "customer" if c.get("author_id") == requester else "agent"
            ts = parse(c.get("created_at"))
            if ts:
                by_key.setdefault((atype, ts.isoformat()), []).append(c)

        matched_here = 0
        for i, m in enumerate(msgs):
            if m.get("author_id"):
                continue
            ts = parse(m.get("timestamp"))
            key = (m.get("author_type"), ts.isoformat() if ts else None)
            cand = by_key.get(key)
            c = None
            if cand:
                c = cand.pop(0)
            elif i < len(pub) and (
                "customer" if pub[i].get("author_id") == requester else "agent"
            ) == m.get("author_type"):
                # positional fallback, only when the author_type agrees
                c = pub[i]
            if c is None:
                stats["msgs_unmatched"] += 1
                continue
            m["author_id"] = str(c.get("author_id") or "")
            seen_ids.add(m["author_id"])
            matched_here += 1
            stats["msgs_matched"] += 1

        if matched_here and not args.dry_run:
            path.write_text(json.dumps(data, indent=2))
        stats["tickets"] += 1
        if stats["tickets"] % 25 == 0:
            print("  %s" % stats, flush=True)
        time.sleep(0.12)

    print()
    print("=== messages ===  %s" % stats)

    # resolve ids -> names
    todo = [i for i in seen_ids if i and i not in directory]
    print()
    print("resolving %d new user ids (%d already known)..." % (len(todo), len(directory)))
    for uid in todo:
        try:
            r = httpx.get(base + "/users/" + uid + ".json", auth=auth, timeout=25)
            if r.status_code != 200:
                directory[uid] = {"name": "(unknown %s)" % uid, "email": "", "role": ""}
                continue
            u = r.json()["user"]
            directory[uid] = {"name": u.get("name") or "", "email": u.get("email") or "",
                              "role": u.get("role") or ""}
        except Exception:
            directory[uid] = {"name": "(lookup failed %s)" % uid, "email": "", "role": ""}
        time.sleep(0.1)

    if not args.dry_run:
        DIRECTORY.parent.mkdir(parents=True, exist_ok=True)
        DIRECTORY.write_text(json.dumps(directory, indent=2))
        print("wrote %s" % DIRECTORY)

    agents = {k: v for k, v in directory.items() if v.get("role") in ("agent", "admin")}
    print()
    print("=== directory: %d users, %d of them agents/admins ===" % (len(directory), len(agents)))
    for uid, v in sorted(agents.items(), key=lambda x: x[1]["name"])[:30]:
        print("  %-20s %-28s %s" % (uid, v["name"][:26], v["role"]))


if __name__ == "__main__":
    main()
