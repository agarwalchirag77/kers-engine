"""Adapter from Zendesk Event Subscription payloads to our internal models.

Zendesk's modern Event Subscriptions send CloudEvents-style envelopes that
look like:

    {
      "id": "<event uuid>",
      "time": "2026-06-09T08:36:44.304Z",
      "type": "zen:event-type:ticket.comment_added",
      "subject": "zen:ticket:71447",
      "detail": { ... full ticket snapshot ... },
      "event":  { ... event-specific payload ... }
    }

This module maps that envelope to either a `MessageEvent`, a `ClosedEvent`,
or a skip decision (with a reason string for logging).

ALL filtering / cleansing of Zendesk events lives here. The webhook layer
just calls `adapt()` and acts on the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

from ...models import ClosedEvent, Message, MessageEvent, TicketMetadata


COMMENT_ADDED = "zen:event-type:ticket.comment_added"
STATUS_CHANGED = "zen:event-type:ticket.status_changed"
TICKET_CREATED = "zen:event-type:ticket.created"
MESSAGING_MESSAGE_ADDED = "zen:event-type:messaging_ticket.message_added"

# The single Zendesk status that means "no more messages can arrive."
# We deliberately ignore SOLVED / PENDING / OPEN / NEW transitions — those
# still allow customer replies.
TERMINAL_STATUS = "CLOSED"


@dataclass
class AdapterResult:
    """Outcome of adapting a raw Event Subscription payload.

    Exactly one of `message`, `closed`, or `skip_reason` will be set.
    """

    message: Optional[MessageEvent] = None
    closed: Optional[ClosedEvent] = None
    skip_reason: Optional[str] = None

    @property
    def event(self) -> Optional[Union[MessageEvent, ClosedEvent]]:
        return self.message or self.closed


def adapt(raw: dict[str, Any]) -> AdapterResult:
    """Map a raw Zendesk Event Subscription payload to an internal event.

    Returns AdapterResult with either:
      - `message` set: caller should enqueue as a message
      - `closed`  set: caller should enqueue as a closed event
      - `skip_reason` set: caller should log + return 200 ignored
    """
    event_type = raw.get("type", "") or ""
    detail = raw.get("detail") or {}
    event_data = raw.get("event") or {}

    if event_type == COMMENT_ADDED:
        return _adapt_comment_added(raw, detail, event_data)

    if event_type == STATUS_CHANGED:
        return _adapt_status_changed(raw, detail, event_data)

    if event_type == MESSAGING_MESSAGE_ADDED:
        return _adapt_messaging_message_added(raw, detail, event_data)

    return AdapterResult(skip_reason=f"unhandled event type: {event_type or '(missing)'}")


def extract_metadata(raw: dict[str, Any]) -> Optional[TicketMetadata]:
    """Pull ticket metadata out of any raw Zendesk Event Subscription payload
    that carries the full ticket detail (ticket.created, ticket.status_changed,
    ticket.comment_added). Returns None for skinny payloads that don't carry
    enough info (messaging_ticket.* events only carry the ticket id).

    The webhook layer uses this opportunistically so that when a messaging
    event arrives later, we already have the group/tags/etc on file.
    """
    detail = raw.get("detail") or {}
    if not detail.get("group_id"):
        return None
    ticket_id = str(detail.get("id") or "")
    if not ticket_id:
        return None
    return TicketMetadata(
        ticket_id=ticket_id,
        subject=detail.get("subject") or "",
        group_id=str(detail.get("group_id") or ""),
        priority=_lower(detail.get("priority")),
        channel=_extract_channel(detail.get("via")),
        tags=list(detail.get("tags") or []),
        locale="en-US",
        requester_id_hash=str(detail.get("requester_id") or ""),
        agent_email="",
    )


def is_bot_actor(actor: Optional[dict[str, Any]]) -> bool:
    """True if a messaging-event `event.actor` represents Zendesk's answer
    bot or any other system/automation source we don't want to score.

    Detection signals (any one is enough):
      - actor.type in {"system", "bot"}
      - actor.id starts with "zd:" (Zendesk's prefix for built-in actors,
        e.g. "zd:answerBot")
      - "bot" appears anywhere in actor.id (covers custom bots)
    """
    if not isinstance(actor, dict):
        return False
    actor_type = (actor.get("type") or "").lower()
    if actor_type in ("system", "bot"):
        return True
    actor_id = (actor.get("id") or "").lower()
    if actor_id.startswith("zd:"):
        return True
    if "bot" in actor_id:
        return True
    return False


def is_bot_messaging_event(raw: dict[str, Any]) -> bool:
    """True iff `raw` is a messaging event whose actor is a bot/system. Used
    at the webhook layer to drop these before any payload-archive or
    metadata-cache side effects."""
    if raw.get("type") != MESSAGING_MESSAGE_ADDED:
        return False
    actor = (raw.get("event") or {}).get("actor")
    return is_bot_actor(actor)


def _adapt_comment_added(
    raw: dict[str, Any], detail: dict[str, Any], event_data: dict[str, Any]
) -> AdapterResult:
    comment = event_data.get("comment") or {}

    # Only act on customer-facing (public) comments. Internal notes, bot
    # chatter, system messages all carry is_public=false.
    if not comment.get("is_public"):
        return AdapterResult(skip_reason="comment is not public")

    body = (comment.get("body") or "").strip()
    if not body:
        return AdapterResult(skip_reason="comment body is empty")

    # author.is_staff: true = agent, false = customer/end-user.
    author = comment.get("author") or {}
    author_type = "agent" if author.get("is_staff") else "customer"
    # Zendesk has put the id in either place depending on event shape; take
    # whichever is present so agent attribution survives both.
    author_id = str(author.get("id") or comment.get("author_id") or "")

    # Prefer the event's own timestamp; fall back to the ticket's updated_at.
    event_ts = _parse_ts(raw.get("time")) or _parse_ts(detail.get("updated_at"))
    if event_ts is None:
        return AdapterResult(skip_reason="could not parse event timestamp")

    msg = Message(
        timestamp=event_ts, author_type=author_type, body=body, author_id=author_id
    )

    return AdapterResult(
        message=MessageEvent(
            event_id=str(raw.get("id") or ""),
            event_timestamp=event_ts,
            ticket_id=str(detail.get("id") or ""),
            subject=detail.get("subject") or "",
            group_id=str(detail.get("group_id") or ""),
            priority=_lower(detail.get("priority")),
            channel=_extract_channel(detail.get("via")),
            tags=list(detail.get("tags") or []),
            locale="en-US",  # not provided by Event Subscriptions
            requester_id_hash=str(detail.get("requester_id") or ""),
            agent_email="",  # not provided by Event Subscriptions
            messages=[msg],
        )
    )


def _adapt_status_changed(
    raw: dict[str, Any], detail: dict[str, Any], event_data: dict[str, Any]
) -> AdapterResult:
    current = (event_data.get("current") or "").upper()

    if current != TERMINAL_STATUS:
        previous = (event_data.get("previous") or "").upper()
        return AdapterResult(
            skip_reason=f"status changed {previous} → {current} (acting only on → {TERMINAL_STATUS})"
        )

    event_ts = _parse_ts(raw.get("time")) or _parse_ts(detail.get("updated_at"))
    if event_ts is None:
        return AdapterResult(skip_reason="could not parse event timestamp")

    return AdapterResult(
        closed=ClosedEvent(
            event_id=str(raw.get("id") or ""),
            event_timestamp=event_ts,
            ticket_id=str(detail.get("id") or ""),
            resolution_outcome=current.lower(),
        )
    )


def _adapt_messaging_message_added(
    raw: dict[str, Any], detail: dict[str, Any], event_data: dict[str, Any]
) -> AdapterResult:
    """Adapter for the chat-widget channel.

    Messaging payloads are MUCH skinnier than ticket.comment_added — they only
    carry the ticket id, actor type ("end_user"|"agent"), and the message body.
    Group/tags/subject must be enriched from the ticket store by the webhook
    layer (populated by earlier ticket.* events for the same ticket).
    """
    actor = event_data.get("actor") or {}
    message = event_data.get("message") or {}

    body = (message.get("body") or "").strip()
    if not body:
        return AdapterResult(skip_reason="message body is empty")

    actor_type_raw = (actor.get("type") or "").lower()
    if actor_type_raw == "end_user":
        author_type = "customer"
    elif actor_type_raw == "agent":
        author_type = "agent"
    else:
        return AdapterResult(
            skip_reason=f"unrecognized actor type: {actor_type_raw!r}"
        )

    event_ts = _parse_ts(raw.get("time"))
    if event_ts is None:
        return AdapterResult(skip_reason="could not parse event timestamp")

    msg = Message(
        timestamp=event_ts,
        author_type=author_type,
        body=body,
        author_id=str(actor.get("id") or ""),
    )

    # group_id / tags left blank — enrichment happens downstream.
    return AdapterResult(
        message=MessageEvent(
            event_id=str(raw.get("id") or ""),
            event_timestamp=event_ts,
            ticket_id=str(detail.get("id") or ""),
            subject="",
            group_id="",
            priority="",
            channel="native_messaging",
            tags=[],
            locale="en-US",
            requester_id_hash=str(actor.get("id") or ""),
            agent_email="",
            messages=[msg],
        )
    )


# --- helpers ---

def _parse_ts(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        # Zendesk messaging events send nanosecond precision (9 digits after
        # the dot). Python's fromisoformat < 3.11 caps at microseconds, so
        # trim any fractional-second tail past 6 digits.
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _lower(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    return v.lower()


def _extract_channel(via: Any) -> str:
    """`detail.via` looks like {"channel": "native_messaging"} or similar."""
    if isinstance(via, dict):
        return via.get("channel") or ""
    if isinstance(via, str):
        return via
    return ""
