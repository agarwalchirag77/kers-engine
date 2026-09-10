from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.storage.db import Database
from app.storage.logger import StructuredLogger
from app.storage.ticket_files import JsonTicketStore
from app.webhook import router as webhook_router
from app.worker import Worker

from .conftest import FakeAIClient, FakeZendeskPusher, make_sentiment


def _make_app(tmp_path, secret: str = None, debounce_seconds: int = 0):
    from app.models import WeightConfig

    config = WeightConfig(
        weights={
            "frustration": 0.15,
            "urgency": 0.12,
            "churn_threat": 0.25,
            "confusion": 0.10,
            "politeness_erosion": 0.05,
            "dissatisfaction_trajectory": 0.13,
            "agent_tone": 0.08,
            "commitment_to_delivery_ratio": 0.12,
        },
        threshold=3.25,
        debounce_seconds=debounce_seconds,
        min_messages_before_eval=6,
    )

    ai = FakeAIClient(make_sentiment(ers_target=3.5))
    zd = FakeZendeskPusher()
    worker = Worker(
        ticket_store=JsonTicketStore(tmp_path / "tickets"),
        ai_client=ai,
        zd_pusher=zd,
        db=Database(tmp_path / "data" / "test.sqlite"),
        logger=StructuredLogger(tmp_path / "logs"),
        weight_config=config,
        model_version="fake-model",
        prompt_version="ers_prompt_v1",
    )

    app = FastAPI()
    app.include_router(webhook_router)

    @app.on_event("startup")
    async def _start():
        app.state.worker = worker
        app.state.webhook_secret = secret
        worker.start()

    @app.on_event("shutdown")
    async def _stop():
        await worker.stop()

    return app, worker, ai, zd


def _payload(ticket_id="t1", event_id="evt-1", msg_count=1):
    return {
        "event_id": event_id,
        "event_timestamp": datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "subject": "Test",
        # Must match an allowed group in app/config/filters.json
        "group_id": "44897999201817",
        "priority": "normal",
        "channel": "email",
        "tags": [],
        "locale": "en-US",
        "requester_id_hash": "abc",
        "agent_email": "agent@x.com",
        "messages": [
            {
                "timestamp": datetime(2026, 6, 1, 10, i, tzinfo=timezone.utc).isoformat(),
                "author_type": "customer" if i % 2 == 0 else "agent",
                "body": f"msg-{i}",
            }
            for i in range(msg_count)
        ],
    }


def test_message_webhook_accepts_valid_payload(tmp_path):
    app, _, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/webhooks/zendesk/message", json=_payload())
        assert r.status_code == 200
        assert r.json() == {"status": "queued"}


def test_message_webhook_rejects_invalid_payload(tmp_path):
    app, _, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/webhooks/zendesk/message", json={"bogus": True})
        assert r.status_code == 400


def test_signature_validation_passes_when_correct(tmp_path):
    secret = "shh"
    app, _, _, _ = _make_app(tmp_path, secret=secret)
    payload = _payload()
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    with TestClient(app) as client:
        r = client.post(
            "/webhooks/zendesk/message",
            content=body,
            headers={
                "X-Zendesk-Webhook-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200


def test_signature_validation_fails_when_wrong(tmp_path):
    app, _, _, _ = _make_app(tmp_path, secret="shh")
    payload = _payload()
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        r = client.post(
            "/webhooks/zendesk/message",
            content=body,
            headers={
                "X-Zendesk-Webhook-Signature": "wrong",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 401


def test_close_webhook_purges(tmp_path):
    """End-to-end: message webhook → eval runs → close webhook → file purged."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        # 6 messages → triggers eval
        r = client.post("/webhooks/zendesk/message", json=_payload(msg_count=6))
        assert r.status_code == 200

        # Give the worker time to process
        import time as _time
        for _ in range(50):
            if zd.pushes:
                break
            _time.sleep(0.05)
        assert zd.pushes, "expected at least one push"

        # Now close
        close_payload = {
            "event_id": "close-1",
            "event_timestamp": datetime(2026, 6, 2, tzinfo=timezone.utc).isoformat(),
            "ticket_id": "t1",
            "resolution_outcome": "solved",
        }
        r2 = client.post("/webhooks/zendesk/closed", json=close_payload)
        assert r2.status_code == 200

        for _ in range(20):
            if not (tmp_path / "tickets" / "t1.json").exists():
                break
            _time.sleep(0.05)
        assert not (tmp_path / "tickets" / "t1.json").exists()


def _zd_event(event_type, *, detail=None, event=None, event_id="evt-1"):
    """Minimal Zendesk Event Subscription envelope for /events tests."""
    return {
        "id": event_id,
        "time": "2026-06-09T08:36:44Z",
        "type": event_type,
        "subject": "zen:ticket:" + (detail or {}).get("id", "1"),
        "detail": detail or {},
        "event": event or {},
    }


def test_events_endpoint_routes_public_comment_to_worker(tmp_path):
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.comment_added",
            detail={
                "id": "t-events-1",
                "subject": "Help",
                "group_id": "44897999201817",  # matches the allowed-groups in filters.json
                "tags": ["vip"],
                "via": {"channel": "email"},
            },
            event={
                "comment": {
                    "body": "I need help",
                    "is_public": True,
                    "author": {"is_staff": False},
                }
            },
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["kind"] == "message"


def test_events_endpoint_skips_private_comment(tmp_path):
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.comment_added",
            detail={"id": "t1", "group_id": "44897999201817", "tags": []},
            event={
                "comment": {
                    "body": "internal",
                    "is_public": False,
                    "author": {"is_staff": True},
                }
            },
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert "not public" in r.json()["reason"]


def test_events_endpoint_routes_status_closed_to_close(tmp_path):
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.status_changed",
            detail={"id": "t-close-1"},
            event={"previous": "SOLVED", "current": "CLOSED"},
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["kind"] == "closed"


def test_events_endpoint_skips_status_change_to_open(tmp_path):
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.status_changed",
            detail={"id": "t1"},
            event={"previous": "NEW", "current": "OPEN"},
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"


def test_events_endpoint_applies_group_filter(tmp_path):
    """Filter still runs after adapter for message events."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.comment_added",
            detail={
                "id": "t1",
                "group_id": "Internal IT",  # NOT in allowed list
                "tags": [],
            },
            event={
                "comment": {
                    "body": "hi",
                    "is_public": True,
                    "author": {"is_staff": False},
                }
            },
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert "allowed_group_ids" in r.json()["reason"]


def test_events_endpoint_saves_payload_for_allowed_group(tmp_path):
    """Payloads from allowed-group tickets are saved to logs/payloads/."""
    app, worker, ai, zd = _make_app(tmp_path)
    payloads_dir = tmp_path / "logs" / "payloads"
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.comment_added",
            detail={
                "id": "t1",
                "group_id": "44897999201817",  # in allowed list
                "tags": [],
            },
            event={
                "comment": {
                    "body": "hi",
                    "is_public": True,
                    "author": {"is_staff": False},
                }
            },
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
    saved = list(payloads_dir.glob("*.json")) if payloads_dir.exists() else []
    assert len(saved) == 1


def test_events_endpoint_does_not_save_payload_for_disallowed_group(tmp_path):
    """Payloads from groups outside allowed_group_ids must NOT be saved."""
    app, worker, ai, zd = _make_app(tmp_path)
    payloads_dir = tmp_path / "logs" / "payloads"
    with TestClient(app) as client:
        payload = _zd_event(
            "zen:event-type:ticket.comment_added",
            detail={
                "id": "t1",
                "group_id": "9999999999999",  # NOT in allowed list
                "tags": [],
            },
            event={
                "comment": {
                    "body": "hi",
                    "is_public": True,
                    "author": {"is_staff": False},
                }
            },
        )
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
    saved = list(payloads_dir.glob("*.json")) if payloads_dir.exists() else []
    assert len(saved) == 0


def test_messaging_event_without_cached_metadata_still_processed(tmp_path):
    """Per policy: messaging events are processed regardless of group. A
    cache miss should NOT block them — only excluded tags can stop them."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = {
            "id": "evt-msg-1",
            "time": "2026-06-10T07:18:24Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "subject": "zen:ticket:99999",
            "detail": {"id": "99999"},
            "event": {
                "actor": {"type": "end_user"},
                "message": {"body": "hi"},
            },
        }
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["kind"] == "message"


def test_messaging_event_enriched_after_ticket_created_seeds_metadata(tmp_path):
    """End-to-end: a ticket.created seeds metadata; a follow-up messaging
    event uses that cache and gets routed to the worker."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        # 1. Send ticket.created with full detail (seeds the cache)
        create_payload = _zd_event(
            "zen:event-type:ticket.created",
            detail={
                "id": "msg-ticket-1",
                "subject": "Chat",
                "group_id": "44897999201817",  # allowed
                "tags": ["bot_chat"],
                "via": {"channel": "native_messaging"},
            },
            event={"meta": {}},
        )
        r1 = client.post("/webhooks/zendesk/events", json=create_payload)
        assert r1.status_code == 200
        # ticket.created itself is "unhandled" by the adapter, but the
        # webhook layer seeds metadata before adapter runs
        assert r1.json()["status"] == "ignored"
        # Confirm the seed happened
        meta = worker.ticket_store.get_metadata("msg-ticket-1")
        assert meta.group_id == "44897999201817"
        assert meta.tags == ["bot_chat"]

        # 2. Send a messaging event for the SAME ticket
        msg_payload = {
            "id": "evt-msg-1",
            "time": "2026-06-10T07:18:24Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "detail": {"id": "msg-ticket-1"},
            "event": {
                "actor": {"type": "end_user"},
                "message": {"body": "hi this is a real message"},
            },
        }
        r2 = client.post("/webhooks/zendesk/events", json=msg_payload)
        assert r2.status_code == 200
        assert r2.json()["status"] == "queued"
        assert r2.json()["kind"] == "message"


def test_messaging_event_passes_even_when_cached_group_disallowed(tmp_path):
    """New policy: messaging tickets ignore the group whitelist. Even if the
    cached group is not in allowed_group_ids, the messaging event proceeds."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        create_payload = _zd_event(
            "zen:event-type:ticket.created",
            detail={
                "id": "msg-ticket-2",
                "group_id": "9999999",  # NOT in allowed list
                "tags": [],
                "via": {"channel": "native_messaging"},
            },
            event={"meta": {}},
        )
        client.post("/webhooks/zendesk/events", json=create_payload)

        msg_payload = {
            "id": "evt-msg-2",
            "time": "2026-06-10T07:18:24Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "detail": {"id": "msg-ticket-2"},
            "event": {
                "actor": {"type": "end_user"},
                "message": {"body": "hi"},
            },
        }
        r = client.post("/webhooks/zendesk/events", json=msg_payload)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["kind"] == "message"


def test_messaging_event_filtered_when_cached_excluded_tag(tmp_path):
    """Messaging events ARE stopped by excluded tags (the only filter that
    applies to chat tickets in the new policy)."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        # Seed with an allowed group, but a spam tag
        create_payload = _zd_event(
            "zen:event-type:ticket.created",
            detail={
                "id": "msg-ticket-spam",
                "group_id": "44897999201817",
                "tags": ["bot_chat", "spam_spam"],
                "via": {"channel": "native_messaging"},
            },
            event={"meta": {}},
        )
        client.post("/webhooks/zendesk/events", json=create_payload)

        msg_payload = {
            "id": "evt-msg-spam",
            "time": "2026-06-10T07:18:24Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "detail": {"id": "msg-ticket-spam"},
            "event": {
                "actor": {"type": "end_user"},
                "message": {"body": "test"},
            },
        }
        r = client.post("/webhooks/zendesk/events", json=msg_payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert "spam_spam" in r.json()["reason"]


def test_payload_saved_per_ticket_as_array(tmp_path):
    """Multiple events for the same ticket land in <ticket_id>_payload.json
    as an appended JSON array."""
    app, worker, ai, zd = _make_app(tmp_path)
    payloads_dir = tmp_path / "logs" / "payloads"
    with TestClient(app) as client:
        # Two events for the same allowed-group ticket
        for ev_id in ("evt-A", "evt-B"):
            payload = _zd_event(
                "zen:event-type:ticket.comment_added",
                event_id=ev_id,
                detail={
                    "id": "my-ticket",
                    "group_id": "44897999201817",
                    "tags": [],
                },
                event={
                    "comment": {
                        "body": f"msg from {ev_id}",
                        "is_public": True,
                        "author": {"is_staff": False},
                    }
                },
            )
            r = client.post("/webhooks/zendesk/events", json=payload)
            assert r.status_code == 200

    files = list(payloads_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == "my-ticket_payload.json"
    import json as _json
    contents = _json.loads(files[0].read_text())
    assert isinstance(contents, list)
    assert len(contents) == 2
    assert contents[0]["id"] == "evt-A"
    assert contents[1]["id"] == "evt-B"


def test_bot_messaging_event_dropped_before_payload_save(tmp_path):
    """Bot/system messaging events get returned 200-ignored at the door —
    no payload file, no ticket file, no adapter call."""
    app, worker, ai, zd = _make_app(tmp_path)
    payloads_dir = tmp_path / "logs" / "payloads"
    tickets_dir = tmp_path / "tickets"
    with TestClient(app) as client:
        payload = {
            "id": "evt-bot-1",
            "time": "2026-06-15T05:47:33Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "subject": "zen:ticket:71726",
            "detail": {"id": "71726"},
            "event": {
                "actor": {"id": "zd:answerBot", "name": "Hevo", "type": "system"},
                "message": {"body": "We feel that issues will be best handled..."},
            },
        }
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert "bot/system actor" in r.json()["reason"]

    # No payload archive was created for this ticket
    saved = list(payloads_dir.glob("71726*.json")) if payloads_dir.exists() else []
    assert len(saved) == 0
    # No ticket file was touched
    assert not (tickets_dir / "71726.json").exists()


def test_human_messaging_event_still_processed(tmp_path):
    """Sanity check: a real end_user message goes through normally."""
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = {
            "id": "evt-human-1",
            "time": "2026-06-15T05:47:33Z",
            "type": "zen:event-type:messaging_ticket.message_added",
            "detail": {"id": "71727"},
            "event": {
                "actor": {"id": "user-abc", "type": "end_user", "name": "Real Person"},
                "message": {"body": "I really need help with my pipeline"},
            },
        }
        r = client.post("/webhooks/zendesk/events", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"


def test_events_endpoint_rejects_malformed_json(tmp_path):
    app, worker, ai, zd = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/webhooks/zendesk/events",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400


def test_health_endpoint(monkeypatch, tmp_path):
    """Sanity check for the real app's /health route."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "test-token")
    monkeypatch.setenv("ERS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ERS_TICKET_DIR", str(tmp_path / "tickets"))
    monkeypatch.setenv("ERS_LOG_DIR", str(tmp_path / "logs"))
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
