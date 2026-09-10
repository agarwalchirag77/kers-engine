"""Restore ticket_files from Zendesk for all currently-active tickets in
KERS's monitored groups.

Runs on the REMOTE server (needs ZENDESK_API_TOKEN in env, and reads
app/config/zendesk.json + app/config/filters.json to know scope).

--dry-run: prints counts only, writes nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent.parent  # ~/kers-engine
sys.path.insert(0, str(ROOT))

ZENDESK_DIR = ROOT / "app" / "connectors" / "zendesk"
ZENDESK_CONFIG = json.loads((ZENDESK_DIR / "settings.json").read_text())
FILTERS = json.loads((ZENDESK_DIR / "filters.json").read_text())
TICKET_FILES_DIR = ROOT / "ticket_files"
TICKET_FILES_DIR.mkdir(exist_ok=True)

SUBDOMAIN = ZENDESK_CONFIG["subdomain"]
BASE = f"https://{SUBDOMAIN}.zendesk.com/api/v2"
API_TOKEN = os.environ.get(ZENDESK_CONFIG["api_token_env_var"])
if not API_TOKEN:
    print(f"ERROR: env var {ZENDESK_CONFIG['api_token_env_var']} not set", file=sys.stderr)
    sys.exit(1)
AUTH = (f"{ZENDESK_CONFIG['api_user']}/token", API_TOKEN)

ALLOWED_GROUP_IDS = list(FILTERS["allowed_group_ids"])
# Restore-time filter: exclude terminal statuses AND anything tagged as
# customer-confirmed-resolved. Both underscore and hyphen variants because
# I don't know which one Hevo actually uses.
EXTRA_EXCLUDED_TAGS = {"solved_confirmed", "solved-confirmed"}
EXCLUDED_TAGS = set(FILTERS.get("excluded_tags", [])) | EXTRA_EXCLUDED_TAGS

DRY_RUN = "--dry-run" in sys.argv


def search_active_tickets(group_id: str):
    page = 1
    while True:
        r = httpx.get(
            f"{BASE}/search.json",
            params={
                # status<closed = new/open/pending/hold/solved (skip closed only)
                "query": f"type:ticket status<closed group:{group_id}",
                "page": page,
                "per_page": 100,
            },
            auth=AUTH,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for result in data.get("results", []):
            if result.get("result_type") == "ticket":
                yield result
        if not data.get("next_page"):
            break
        page += 1
        time.sleep(0.2)


STAFF_ROLES = ("agent", "admin")
STAFF_EMAIL_DOMAIN = "@hevodata.com"


def _is_staff(user: dict) -> bool:
    """Staff iff a Hevo email OR a staff role — neither alone is enough.

    Classifying by "is this the ticket requester" mislabelled every CC'd
    colleague and second engineer on the customer side as Hevo staff:
    12.6% of messages across 17% of tickets.

    But Zendesk's `role` is not reliable either — six serving agents in
    this instance carry role=end-user on their user records. Trusting role
    alone would have flipped 173 genuine agent messages into customer
    messages, which is worse than the original bug.

    The two signals disagree in one direction only: no external address is
    ever marked staff. So their union is safe.
    """
    email = (user.get("email") or "").lower()
    if email.endswith(STAFF_EMAIL_DOMAIN):
        return True
    return (user.get("role") or "") in STAFF_ROLES


def fetch_comments(ticket_id: int):
    """Comments plus the `users` sideload, so authors can be classified."""
    r = httpx.get(
        f"{BASE}/tickets/{ticket_id}/comments.json",
        params={"include": "users"},
        auth=AUTH,
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    staff = {
        str(u["id"]): _is_staff(u)
        for u in payload.get("users", [])
        if u.get("id") is not None
    }
    return payload.get("comments", []), staff


def to_ticket_file(ticket: dict, comments: list, requester_id, staff: dict) -> dict:
    messages = []
    for c in comments:
        if not c.get("public"):
            continue
        body = (c.get("body") or "").strip()
        if not body:
            continue
        aid = str(c.get("author_id") or "")
        if aid in staff:
            author_type = "agent" if staff[aid] else "customer"
        else:
            # Author absent from the sideload (e.g. synthetic id -1 used for
            # system-generated chat transcripts). Fall back to the old guess.
            author_type = "customer" if c.get("author_id") == requester_id else "agent"
        messages.append({
            "author_type": author_type,
            "author_id": str(c.get("author_id") or ""),
            "body": body,
            "timestamp": c.get("created_at"),
        })
    via = ticket.get("via") or {}
    return {
        "ticket_id": str(ticket["id"]),
        "subject": ticket.get("subject") or "",
        "group_id": str(ticket.get("group_id") or ""),
        "priority": ticket.get("priority") or "",
        "channel": via.get("channel") or "",
        "tags": ticket.get("tags") or [],
        "locale": "en-US",
        "requester_id_hash": str(requester_id or ""),
        "agent_email": "",
        "created_at": ticket.get("created_at"),
        "last_updated_at": ticket.get("updated_at"),
        "messages": messages,
    }


def main():
    print(f"mode: {'DRY RUN (no writes)' if DRY_RUN else 'LIVE (will write ticket_files/*.json)'}")
    print(f"groups: {ALLOWED_GROUP_IDS}")
    print(f"excluded tags: {EXCLUDED_TAGS}")
    print()

    all_tickets = {}
    for gid in ALLOWED_GROUP_IDS:
        print(f"searching group {gid}...", flush=True)
        found = 0
        try:
            for t in search_active_tickets(gid):
                all_tickets[t["id"]] = t
                found += 1
        except httpx.HTTPStatusError as e:
            print(f"  HTTP error: {e.response.status_code} {e.response.text[:200]}")
            continue
        print(f"  found {found} active tickets in group {gid}")

    print()
    print(f"total unique active tickets: {len(all_tickets)}")
    by_status = {}
    for t in all_tickets.values():
        by_status[t.get("status", "?")] = by_status.get(t.get("status", "?"), 0) + 1
    print(f"by status: {by_status}")

    tag_excluded = [t for t in all_tickets.values() if set(t.get("tags") or []) & EXCLUDED_TAGS]
    print(f"would be tag-excluded: {len(tag_excluded)}")

    if DRY_RUN:
        print()
        print("dry run complete. rerun without --dry-run to fetch comments and write files.")
        return

    print()
    print("fetching comments and writing ticket_files...")
    stats = {"seen": 0, "written": 0, "skipped_tag": 0, "skipped_no_msgs": 0, "errors": 0}
    for t in all_tickets.values():
        stats["seen"] += 1
        if set(t.get("tags") or []) & EXCLUDED_TAGS:
            stats["skipped_tag"] += 1
            continue
        try:
            comments, staff = fetch_comments(t["id"])
        except Exception as e:
            print(f"  err {t['id']}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue
        tf = to_ticket_file(t, comments, t.get("requester_id"), staff)
        if not tf["messages"]:
            stats["skipped_no_msgs"] += 1
            continue
        (TICKET_FILES_DIR / f"{tf['ticket_id']}.json").write_text(json.dumps(tf, indent=2))
        stats["written"] += 1
        if stats["seen"] % 25 == 0:
            print(f"  progress {stats}", flush=True)
        time.sleep(0.15)

    print()
    print(f"DONE  {stats}")
    print(f"ticket_files on disk now: {len(list(TICKET_FILES_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
