from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import ValidationError

from .filtering import FilterConfig, load_filter_config, should_process
from .models import ClosedEvent, MessageEvent
from .storage.ticket_files import TicketNotFoundError
from .worker import Worker
from .connectors.zendesk.adapter import adapt, extract_metadata, is_bot_messaging_event


router = APIRouter(prefix="/webhooks/zendesk")

# Loaded once at module import; cheap to read again on startup if changed.
_FILTER_CONFIG: FilterConfig = load_filter_config()


def _verify_signature(body: bytes, signature: Optional[str], secret: Optional[str]) -> bool:
    """HMAC SHA256 verification. If no secret is configured, skip (dev mode)."""
    if not secret:
        return True
    if not signature:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def _get_worker(request: Request) -> Worker:
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        raise HTTPException(status_code=503, detail="worker not initialized")
    return worker


def _get_secret(request: Request) -> Optional[str]:
    return getattr(request.app.state, "webhook_secret", None)


def _save_ticket_payload(
    log_dir: Path, ticket_id: str, parsed: dict
) -> Optional[str]:
    """Append the parsed event to a per-ticket JSON array file.

    Filename: <ticket_id>_payload.json
    Content:  JSON array of events for that ticket, oldest first.

    Re-reads the existing array on every call and rewrites the whole file.
    Fine for our volume (small files, few-dozen events per ticket).
    """
    if not ticket_id:
        return None
    try:
        payload_dir = Path(log_dir) / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{ticket_id}_payload.json"
        path = payload_dir / filename

        if path.exists():
            try:
                existing = json.loads(path.read_text())
                if not isinstance(existing, list):
                    existing = [existing]  # legacy single-payload, wrap
            except (json.JSONDecodeError, OSError):
                existing = []
        else:
            existing = []

        existing.append(parsed)
        path.write_text(json.dumps(existing, indent=2, default=str))
        return filename
    except OSError:
        return None


def _save_unparseable(body: bytes, log_dir: Path) -> Optional[str]:
    """Fallback save path for payloads that fail JSON parse. We can't key
    these by ticket_id (we don't know it), so they get a timestamp-named
    file in a separate subfolder."""
    try:
        payload_dir = Path(log_dir) / "payloads" / "_unparseable"
        payload_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        filename = f"{ts}.bin"
        (payload_dir / filename).write_bytes(body)
        return f"_unparseable/{filename}"
    except OSError:
        return None


@router.post("/events")
async def receive_zendesk_event(
    request: Request,
    x_zendesk_webhook_signature: Optional[str] = Header(default=None),
):
    """Single endpoint for Zendesk Event Subscriptions (CloudEvents format).

    Subscribe this URL to:
      - ticket.comment_added   → routed as a message
      - ticket.status_changed  → routed as closed when current status == CLOSED

    All other event types and intermediate status changes are skipped with a
    logged reason; the response is 200 so Zendesk doesn't retry.
    """
    body = await request.body()
    secret = _get_secret(request)
    if not _verify_signature(body, x_zendesk_webhook_signature, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    worker = _get_worker(request)

    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        # Parse failure — can't key by ticket_id, save with timestamp.
        saved_payload_file = _save_unparseable(body, worker.logger.log_dir)
        worker.logger.error(
            stage="webhook_validation_failed",
            error=str(e)[:1500],
            extra={
                "body_preview": body[:2000].decode("utf-8", errors="replace"),
                "payload_file": saved_payload_file,
                "kind": "zendesk_event",
            },
        )
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

    # Filter bot/system messaging events at the door — before any payload
    # archive, metadata seeding, or adapter work. These are answerBot routing
    # messages and similar automation; they don't belong in the ticket file,
    # the per-ticket payload archive, or anywhere in our scoring pipeline.
    if is_bot_messaging_event(raw):
        actor = (raw.get("event") or {}).get("actor") or {}
        worker.logger.info(
            stage="event_skipped",
            extra={
                "reason": "bot/system actor",
                "actor_id": actor.get("id"),
                "actor_type": actor.get("type"),
                "raw_type": raw.get("type"),
                "raw_event_id": raw.get("id"),
                "ticket_id": (raw.get("detail") or {}).get("id"),
            },
        )
        return {
            "status": "ignored",
            "reason": f"bot/system actor: {actor.get('id') or actor.get('type') or 'unknown'}",
        }

    # Opportunistically seed/refresh ticket metadata from any payload that
    # carries the full ticket detail (ticket.created, ticket.status_changed,
    # ticket.comment_added). Messaging events (skinny payload) will look this
    # up later for group/tags filtering.
    seed = extract_metadata(raw)
    if seed:
        try:
            worker.ticket_store.upsert_metadata(seed.ticket_id, seed)
        except Exception as e:
            worker.logger.warn(
                stage="metadata_seed_failed",
                ticket_id=seed.ticket_id,
                error=str(e)[:300],
            )

    # Decide whether to persist this payload — and if so, append to the
    # per-ticket file.
    #
    # Rules (match the processing-filter rules below):
    #   - Messaging events (chat): save unless an excluded tag is present.
    #     No group check — chat tickets bypass group filtering by policy.
    #   - Regular ticket events: save only if group_id is allowed AND no
    #     excluded tag present.
    #   - Unknown event types: save anyway for debugging visibility.
    known_event_types = {
        "zen:event-type:ticket.comment_added",
        "zen:event-type:ticket.status_changed",
        "zen:event-type:ticket.created",
        "zen:event-type:messaging_ticket.message_added",
    }
    event_type = raw.get("type", "")
    is_messaging = event_type == "zen:event-type:messaging_ticket.message_added"
    ticket_id = str((raw.get("detail") or {}).get("id") or "")

    # Compute effective group/tags — from payload (ticket events) or cache
    # (messaging events).
    if is_messaging:
        try:
            cached = worker.ticket_store.get_metadata(ticket_id)
            effective_group_id = cached.group_id
            effective_tags = cached.tags
        except (TicketNotFoundError, Exception):
            effective_group_id = ""
            effective_tags = []
    else:
        detail = raw.get("detail") or {}
        effective_group_id = str(detail.get("group_id") or "")
        effective_tags = list(detail.get("tags") or [])

    has_excluded_tag = bool(
        set(effective_tags) & set(_FILTER_CONFIG.excluded_tags)
    )
    group_allowed = (
        not _FILTER_CONFIG.allowed_group_ids
        or effective_group_id in _FILTER_CONFIG.allowed_group_ids
    )

    if event_type not in known_event_types:
        should_save = True  # unknown type — always save for debugging
    elif is_messaging:
        should_save = not has_excluded_tag  # messaging: tag filter only
    else:
        should_save = group_allowed and not has_excluded_tag

    saved_payload_file = (
        _save_ticket_payload(worker.logger.log_dir, ticket_id, raw)
        if should_save and ticket_id
        else None
    )

    result = adapt(raw)

    # --- skip path: adapter chose not to act ---
    if result.skip_reason is not None:
        worker.logger.info(
            stage="event_skipped",
            extra={
                "reason": result.skip_reason,
                "raw_type": raw.get("type"),
                "raw_event_id": raw.get("id"),
                "ticket_id": (raw.get("detail") or {}).get("id"),
                "payload_file": saved_payload_file,
                "body_preview": body[:3000].decode("utf-8", errors="replace"),
            },
        )
        return {"status": "ignored", "reason": result.skip_reason}

    # --- message path ---
    if result.message is not None:
        event = result.message

        # Messaging events come in skinny (no group_id/tags). Try to enrich
        # from the ticket store. Unlike before, a cache miss does NOT skip
        # the event — messaging tickets are processed regardless of group,
        # so missing metadata just means weaker DB context for that row.
        enriched_from_cache = False
        if not event.group_id:
            try:
                stored = worker.ticket_store.get_metadata(event.ticket_id)
                event.group_id = stored.group_id
                event.tags = stored.tags or event.tags
                event.subject = stored.subject or event.subject
                event.priority = stored.priority or event.priority
                event.channel = stored.channel or event.channel
                enriched_from_cache = True
            except TicketNotFoundError:
                pass  # messaging tickets proceed without enrichment

        is_messaging_channel = (
            event.channel == "native_messaging"
            or raw.get("type") == "zen:event-type:messaging_ticket.message_added"
        )

        worker.logger.info(
            stage="webhook_received",
            ticket_id=event.ticket_id,
            extra={
                "event_id": event.event_id,
                "kind": "message",
                "source": "event_subscription",
                "payload_file": saved_payload_file,
                "enriched_from_cache": enriched_from_cache,
                "is_messaging": is_messaging_channel,
            },
        )

        # For messaging events, skip the group whitelist check by policy.
        # Only excluded_tags can stop a chat ticket from being processed.
        allow, reason = should_process(
            event, _FILTER_CONFIG, check_group=not is_messaging_channel
        )
        if not allow:
            worker.logger.info(
                stage="eval_skipped_filtered",
                ticket_id=event.ticket_id,
                extra={
                    "reason": reason,
                    "group_id": event.group_id,
                    "tags": event.tags,
                    "payload_file": saved_payload_file,
                    "body_preview": body[:3000].decode("utf-8", errors="replace"),
                },
            )
            return {"status": "ignored", "reason": reason}

        await worker.enqueue_message(event)
        return {"status": "queued", "kind": "message"}

    # --- closed path ---
    if result.closed is not None:
        event_closed = result.closed
        worker.logger.info(
            stage="webhook_received",
            ticket_id=event_closed.ticket_id,
            extra={
                "event_id": event_closed.event_id,
                "kind": "closed",
                "source": "event_subscription",
            },
        )
        await worker.enqueue_close(event_closed)
        return {"status": "queued", "kind": "closed"}

    # Shouldn't get here — adapter contract guarantees one of the three fields.
    worker.logger.error(
        stage="adapter_returned_empty_result",
        extra={"raw_type": raw.get("type")},
    )
    return {"status": "ignored", "reason": "adapter returned empty result"}


@router.post("/message")
async def receive_message(
    request: Request,
    x_zendesk_webhook_signature: Optional[str] = Header(default=None),
):
    body = await request.body()
    secret = _get_secret(request)
    if not _verify_signature(body, x_zendesk_webhook_signature, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        data = json.loads(body)
        event = MessageEvent.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        # Log the failure so we can diagnose without checking Zendesk's
        # Activity tab. Stores the raw body (truncated) and error details
        # in the JSONL log under stage=webhook_validation_failed.
        worker_for_log = getattr(request.app.state, "worker", None)
        if worker_for_log is not None:
            worker_for_log.logger.error(
                stage="webhook_validation_failed",
                error=str(e)[:1500],
                extra={
                    "body_preview": body[:2000].decode("utf-8", errors="replace"),
                    "kind": "message",
                },
            )
        raise HTTPException(status_code=400, detail=f"invalid payload: {e}")

    worker = _get_worker(request)
    worker.logger.info(
        stage="webhook_received",
        ticket_id=event.ticket_id,
        extra={"event_id": event.event_id, "kind": "message"},
    )

    allow, reason = should_process(event, _FILTER_CONFIG)
    if not allow:
        worker.logger.info(
            stage="eval_skipped_filtered",
            ticket_id=event.ticket_id,
            extra={
                "reason": reason,
                "group_id": event.group_id,
                "tags": event.tags,
            },
        )
        return {"status": "ignored", "reason": reason}

    await worker.enqueue_message(event)
    return {"status": "queued"}


@router.post("/closed")
async def receive_close(
    request: Request,
    x_zendesk_webhook_signature: Optional[str] = Header(default=None),
):
    body = await request.body()
    secret = _get_secret(request)
    if not _verify_signature(body, x_zendesk_webhook_signature, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        data = json.loads(body)
        event = ClosedEvent.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        worker_for_log = getattr(request.app.state, "worker", None)
        if worker_for_log is not None:
            worker_for_log.logger.error(
                stage="webhook_validation_failed",
                error=str(e)[:1500],
                extra={
                    "body_preview": body[:2000].decode("utf-8", errors="replace"),
                    "kind": "closed",
                },
            )
        raise HTTPException(status_code=400, detail=f"invalid payload: {e}")

    worker = _get_worker(request)
    worker.logger.info(
        stage="webhook_received",
        ticket_id=event.ticket_id,
        extra={"event_id": event.event_id, "kind": "closed"},
    )
    await worker.enqueue_close(event)
    return {"status": "queued"}
