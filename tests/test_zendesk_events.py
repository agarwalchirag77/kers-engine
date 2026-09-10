"""Unit tests for the Zendesk Event Subscription adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.connectors.zendesk.adapter import (
    _parse_ts,
    adapt,
    COMMENT_ADDED,
    MESSAGING_MESSAGE_ADDED,
    STATUS_CHANGED,
    extract_metadata,
    is_bot_actor,
    is_bot_messaging_event,
)


def _envelope(event_type: str, *, detail: dict = None, event: dict = None) -> dict:
    """Build a minimal Zendesk Event Subscription envelope."""
    return {
        "id": "evt-uuid-1",
        "time": "2026-06-09T08:36:44.304Z",
        "type": event_type,
        "subject": "zen:ticket:71447",
        "detail": detail or {},
        "event": event or {},
    }


# --- comment_added ---


def test_public_customer_comment_yields_message():
    raw = _envelope(
        COMMENT_ADDED,
        detail={
            "id": "71447",
            "subject": "Help me",
            "group_id": "44897999201817",
            "priority": "LOW",
            "tags": ["vip", "billing"],
            "via": {"channel": "email"},
            "requester_id": "12345",
            "updated_at": "2026-06-09T08:36:44Z",
        },
        event={
            "comment": {
                "id": "c1",
                "body": "Hi, my dashboard is broken",
                "is_public": True,
                "author": {"id": "12345", "is_staff": False, "name": "X"},
            }
        },
    )
    result = adapt(raw)
    assert result.message is not None
    assert result.closed is None
    assert result.skip_reason is None

    m = result.message
    assert m.ticket_id == "71447"
    assert m.event_id == "evt-uuid-1"
    assert m.subject == "Help me"
    assert m.group_id == "44897999201817"
    assert m.priority == "low"  # normalized
    assert m.channel == "email"
    assert m.tags == ["vip", "billing"]
    assert len(m.messages) == 1
    assert m.messages[0].author_type == "customer"
    assert m.messages[0].body == "Hi, my dashboard is broken"


def test_public_agent_comment_yields_agent_message():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1", "group_id": "g1", "tags": []},
        event={
            "comment": {
                "body": "Looking into this now",
                "is_public": True,
                "author": {"is_staff": True},
            }
        },
    )
    result = adapt(raw)
    assert result.message is not None
    assert result.message.messages[0].author_type == "agent"


def test_private_comment_skipped():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1"},
        event={
            "comment": {
                "body": "Internal note",
                "is_public": False,
                "author": {"is_staff": True},
            }
        },
    )
    result = adapt(raw)
    assert result.skip_reason == "comment is not public"
    assert result.message is None


def test_empty_body_comment_skipped():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1"},
        event={
            "comment": {
                "body": "   ",
                "is_public": True,
                "author": {"is_staff": False},
            }
        },
    )
    result = adapt(raw)
    assert result.skip_reason == "comment body is empty"


# --- status_changed ---


def test_status_changed_to_closed_yields_closed_event():
    raw = _envelope(
        STATUS_CHANGED,
        detail={"id": "71447"},
        event={"previous": "SOLVED", "current": "CLOSED"},
    )
    result = adapt(raw)
    assert result.closed is not None
    assert result.message is None
    assert result.closed.ticket_id == "71447"
    assert result.closed.resolution_outcome == "closed"


def test_status_changed_to_open_skipped():
    raw = _envelope(
        STATUS_CHANGED,
        detail={"id": "71447"},
        event={"previous": "NEW", "current": "OPEN"},
    )
    result = adapt(raw)
    assert result.closed is None
    assert "OPEN" in result.skip_reason
    assert "CLOSED" in result.skip_reason


def test_status_changed_to_solved_skipped():
    """SOLVED is NOT closed — customer can still re-open."""
    raw = _envelope(
        STATUS_CHANGED,
        detail={"id": "71447"},
        event={"previous": "PENDING", "current": "SOLVED"},
    )
    result = adapt(raw)
    assert result.closed is None
    assert "SOLVED" in result.skip_reason


def test_status_changed_case_insensitive():
    """Zendesk sometimes emits status in lowercase; we still want it to match."""
    raw = _envelope(
        STATUS_CHANGED,
        detail={"id": "71447"},
        event={"previous": "solved", "current": "closed"},
    )
    result = adapt(raw)
    assert result.closed is not None
    assert result.closed.resolution_outcome == "closed"


# --- other event types ---


def test_ticket_created_skipped():
    raw = _envelope(
        "zen:event-type:ticket.created", detail={"id": "1"}, event={"meta": {}}
    )
    result = adapt(raw)
    assert result.skip_reason is not None
    assert "unhandled" in result.skip_reason.lower()


def test_unknown_event_type_skipped():
    raw = _envelope("zen:event-type:something.new", detail={"id": "1"})
    result = adapt(raw)
    assert result.skip_reason is not None


def test_missing_type_field_skipped():
    raw = {"id": "1", "detail": {}, "event": {}}
    result = adapt(raw)
    assert result.skip_reason is not None
    assert "missing" in result.skip_reason.lower() or "unhandled" in result.skip_reason.lower()


# --- robustness: malformed input ---


def test_completely_empty_dict_does_not_crash():
    result = adapt({})
    assert result.skip_reason is not None


def test_missing_detail_does_not_crash_comment():
    raw = {
        "id": "x",
        "time": "2026-06-09T08:36:44Z",
        "type": COMMENT_ADDED,
        "event": {"comment": {"body": "hi", "is_public": True, "author": {}}},
    }
    result = adapt(raw)
    assert result.message is not None
    assert result.message.ticket_id == ""  # missing detail → empty ticket_id, still parses


def test_missing_event_does_not_crash_comment():
    raw = _envelope(COMMENT_ADDED, detail={"id": "1"})
    result = adapt(raw)
    # No event.comment → no is_public field at all → counts as not-public
    assert result.skip_reason == "comment is not public"


def test_null_priority_becomes_empty_string():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1", "priority": None, "tags": []},
        event={
            "comment": {
                "body": "hi",
                "is_public": True,
                "author": {"is_staff": False},
            }
        },
    )
    result = adapt(raw)
    assert result.message is not None
    assert result.message.priority == ""


def test_unparseable_timestamp_falls_back_to_updated_at():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1", "updated_at": "2026-06-09T08:36:44Z"},
        event={
            "comment": {
                "body": "hi",
                "is_public": True,
                "author": {"is_staff": False},
            }
        },
    )
    raw["time"] = "nonsense"
    result = adapt(raw)
    assert result.message is not None
    assert result.message.event_timestamp.year == 2026


def test_parse_ts_accepts_nanosecond_precision():
    """Zendesk messaging events send 9-digit fractional seconds. Python 3.9's
    fromisoformat caps at 6 digits; the parser must trim before delegating."""
    result = _parse_ts("2026-07-09T08:38:27.200000047Z")
    assert result is not None
    assert result.year == 2026 and result.month == 7 and result.day == 9
    assert result.microsecond == 200000  # trimmed from .200000047


def test_parse_ts_still_handles_microsecond_and_no_fractional():
    # The shapes we've seen from Zendesk in production: 6-digit microsecond,
    # 3-digit millisecond, and no fractional at all.
    assert _parse_ts("2026-07-09T08:38:27.123456Z") is not None
    assert _parse_ts("2026-07-09T08:38:27.304Z") is not None
    assert _parse_ts("2026-07-09T08:38:27Z") is not None


def test_adapt_messaging_event_with_nanosecond_ts_yields_message():
    """The exact shape that was dropping messages on ticket 73053."""
    raw = {
        "id": "evt-ns-1",
        "time": "2026-07-09T08:38:27.200000047Z",
        "type": MESSAGING_MESSAGE_ADDED,
        "subject": "zen:ticket:73053",
        "detail": {"id": "73053"},
        "event": {
            "actor": {"type": "end_user", "id": "6a4f5df5dae152f024959a1a"},
            "message": {"body": "Unable to login", "id": "msg-1"},
        },
    }
    result = adapt(raw)
    assert result.message is not None, f"skipped: {result.skip_reason}"
    assert result.message.messages[0].body == "Unable to login"
    assert result.message.messages[0].author_type == "customer"


def test_no_timestamp_anywhere_yields_skip():
    raw = _envelope(
        COMMENT_ADDED,
        detail={"id": "1"},
        event={
            "comment": {
                "body": "hi",
                "is_public": True,
                "author": {"is_staff": False},
            }
        },
    )
    raw["time"] = None
    result = adapt(raw)
    assert result.skip_reason is not None
    assert "timestamp" in result.skip_reason.lower()


# --- real payloads we've actually seen in production ---


def test_real_payload_from_failing_log_comment_added():
    """The actual ticket.comment_added payload that was failing earlier."""
    raw = {
        "account_id": 2222096,
        "detail": {
            "actor_id": "58789189659033",
            "assignee_id": None,
            "brand_id": "360000146293",
            "created_at": "2026-06-09T08:36:43Z",
            "custom_status": "8593966",
            "description": "Conversation with Web User",
            "external_id": None,
            "form_id": "50124257287449",
            "group_id": "44897999201817",
            "id": "71447",
            "is_public": False,
            "organization_id": None,
            "priority": None,
            "requester_id": "58789189659033",
            "status": "OPEN",
            "subject": "Conversation with Khushi Singh",
            "submitter_id": "58789189659033",
            "tags": ["bot_chat", "not_a_reopen"],
            "type": "INCIDENT",
            "updated_at": "2026-06-09T08:36:44Z",
            "via": {"channel": "native_messaging"},
        },
        "event": {
            "comment": {
                "author": {
                    "id": "58789189659033",
                    "is_staff": False,
                    "name": "Khushi Singh",
                },
                "body": "Conversation with Web User 6a27d07ec70c5f1aa576a17b",
                "html_body": "<div>...</div>",
                "id": "58789194475545",
                "is_public": False,  # <- private; should skip
            },
            "meta": {"sequence": {"id": "...", "position": 2}},
        },
        "id": "d8551ba3-4243-433c-8629-aa79dc3e7182",
        "subject": "zen:ticket:71447",
        "time": "2026-06-09T08:36:44.304701605Z",
        "type": COMMENT_ADDED,
        "zendesk_event_version": "2022-11-06",
    }
    result = adapt(raw)
    assert result.skip_reason == "comment is not public"


# --- messaging_ticket.message_added ---


def test_messaging_customer_message_yields_message_event():
    raw = {
        "id": "01KTR6B23G65XY9HMFCRX19C9A",
        "time": "2026-06-10T07:18:24.265Z",
        "type": MESSAGING_MESSAGE_ADDED,
        "subject": "zen:ticket:71520",
        "detail": {"id": "71520"},  # NB: skinny — no group_id!
        "event": {
            "actor": {"id": "user-abc", "name": "X", "type": "end_user"},
            "conversation_id": "conv-123",
            "message": {"body": "thanks that helps", "id": "msg-1"},
        },
    }
    result = adapt(raw)
    assert result.message is not None
    m = result.message
    assert m.ticket_id == "71520"
    assert m.group_id == ""  # to be enriched downstream
    assert m.tags == []
    assert m.channel == "native_messaging"
    assert m.messages[0].author_type == "customer"
    assert m.messages[0].body == "thanks that helps"


def test_messaging_agent_message_yields_agent():
    raw = {
        "id": "evt-1",
        "time": "2026-06-10T07:18:24Z",
        "type": MESSAGING_MESSAGE_ADDED,
        "detail": {"id": "71520"},
        "event": {
            "actor": {"type": "agent"},
            "message": {"body": "let me check"},
        },
    }
    result = adapt(raw)
    assert result.message is not None
    assert result.message.messages[0].author_type == "agent"


def test_messaging_empty_body_skipped():
    raw = {
        "id": "evt-1",
        "time": "2026-06-10T07:18:24Z",
        "type": MESSAGING_MESSAGE_ADDED,
        "detail": {"id": "1"},
        "event": {
            "actor": {"type": "end_user"},
            "message": {"body": "   "},
        },
    }
    result = adapt(raw)
    assert result.skip_reason == "message body is empty"


def test_messaging_unknown_actor_type_skipped():
    raw = {
        "id": "evt-1",
        "time": "2026-06-10T07:18:24Z",
        "type": MESSAGING_MESSAGE_ADDED,
        "detail": {"id": "1"},
        "event": {
            "actor": {"type": "bot"},  # not end_user / agent
            "message": {"body": "hi"},
        },
    }
    result = adapt(raw)
    assert result.skip_reason is not None
    assert "actor type" in result.skip_reason


# --- extract_metadata ---


def test_extract_metadata_from_ticket_comment_added():
    raw = _envelope(
        COMMENT_ADDED,
        detail={
            "id": "71520",
            "subject": "Hi there",
            "group_id": "44897999201817",
            "priority": "HIGH",
            "tags": ["vip", "billing"],
            "via": {"channel": "email"},
            "requester_id": "u-1",
        },
        event={
            "comment": {"body": "x", "is_public": True, "author": {"is_staff": False}}
        },
    )
    md = extract_metadata(raw)
    assert md is not None
    assert md.ticket_id == "71520"
    assert md.group_id == "44897999201817"
    assert md.tags == ["vip", "billing"]
    assert md.priority == "high"
    assert md.channel == "email"


def test_extract_metadata_returns_none_for_skinny_messaging_payload():
    raw = {
        "id": "evt-1",
        "type": MESSAGING_MESSAGE_ADDED,
        "detail": {"id": "71520"},  # no group_id
        "event": {"actor": {"type": "end_user"}, "message": {"body": "hi"}},
    }
    assert extract_metadata(raw) is None


def test_extract_metadata_returns_none_when_group_id_missing():
    raw = _envelope(COMMENT_ADDED, detail={"id": "1"})  # no group_id
    assert extract_metadata(raw) is None


# --- is_bot_actor / is_bot_messaging_event ---


def test_is_bot_actor_system_type():
    assert is_bot_actor({"type": "system", "id": "zd:answerBot", "name": "Hevo"}) is True


def test_is_bot_actor_bot_type():
    assert is_bot_actor({"type": "bot", "id": "custom-1"}) is True


def test_is_bot_actor_zd_prefix():
    """Even if Zendesk changes the type label, the zd: prefix on the id flags it."""
    assert is_bot_actor({"type": "automation", "id": "zd:routingBot"}) is True


def test_is_bot_actor_bot_substring_in_id():
    assert is_bot_actor({"type": "agent", "id": "supportbot-prod"}) is True


def test_is_bot_actor_end_user():
    assert is_bot_actor({"type": "end_user", "id": "abc123"}) is False


def test_is_bot_actor_agent():
    assert is_bot_actor({"type": "agent", "id": "57608919901337"}) is False


def test_is_bot_actor_none_and_empty():
    assert is_bot_actor(None) is False
    assert is_bot_actor({}) is False


def test_is_bot_messaging_event_real_payload():
    """The exact answerBot payload Khushi posted."""
    raw = {
        "type": MESSAGING_MESSAGE_ADDED,
        "event": {
            "actor": {"id": "zd:answerBot", "name": "Hevo", "type": "system"},
            "message": {"body": "We feel that issues will be best handled..."},
        },
    }
    assert is_bot_messaging_event(raw) is True


def test_is_bot_messaging_event_end_user_passes():
    raw = {
        "type": MESSAGING_MESSAGE_ADDED,
        "event": {"actor": {"type": "end_user", "id": "abc"}, "message": {"body": "hi"}},
    }
    assert is_bot_messaging_event(raw) is False


def test_is_bot_messaging_event_not_messaging():
    """The bot filter only applies to messaging events. ticket.comment_added
    events use a different filter path (is_public, is_staff)."""
    raw = {
        "type": COMMENT_ADDED,
        "event": {"actor": {"type": "system", "id": "zd:answerBot"}},
    }
    assert is_bot_messaging_event(raw) is False


def test_real_payload_status_changed_to_open():
    raw = {
        "account_id": 2222096,
        "detail": {"id": "71447", "tags": []},
        "event": {"previous": "NEW", "current": "OPEN"},
        "id": "e9a4cf0b-3967-4ece-9526-84931e102cff",
        "time": "2026-06-09T08:36:44.304701605Z",
        "type": STATUS_CHANGED,
    }
    result = adapt(raw)
    assert result.closed is None
    assert "OPEN" in result.skip_reason
