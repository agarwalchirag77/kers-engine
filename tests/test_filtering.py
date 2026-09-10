from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.filtering import FilterConfig, load_filter_config, should_process
from app.models import Message, MessageEvent


def _make_event(*, group_id: str = "Support L1", tags=None) -> MessageEvent:
    return MessageEvent(
        event_id="evt-1",
        event_timestamp=datetime(2026, 6, 5, 10, 30, tzinfo=timezone.utc),
        ticket_id="t1",
        subject="Test",
        group_id=group_id,
        priority="normal",
        channel="email",
        tags=tags if tags is not None else [],
        locale="en-US",
        requester_id_hash="abc",
        agent_email="agent@x.com",
        messages=[
            Message(
                timestamp=datetime(2026, 6, 5, 10, 30, tzinfo=timezone.utc),
                author_type="customer",
                body="hi",
            )
        ],
    )


# --- group filtering ---


def test_allowed_group_passes():
    config = FilterConfig(allowed_group_ids=["Support L1", "Support L2"])
    allow, reason = should_process(_make_event(group_id="Support L1"), config)
    assert allow is True
    assert reason is None


def test_disallowed_group_filtered():
    config = FilterConfig(allowed_group_ids=["Support L1", "Support L2"])
    allow, reason = should_process(_make_event(group_id="Internal IT"), config)
    assert allow is False
    assert "Internal IT" in reason
    assert "allowed_group_ids" in reason


def test_empty_allowed_groups_accepts_all():
    """allowed_group_ids=[] means accept any group."""
    config = FilterConfig(allowed_group_ids=[], excluded_tags=[])
    allow, _ = should_process(_make_event(group_id="anything"), config)
    assert allow is True


# --- tag filtering ---


def test_excluded_tag_filtered():
    config = FilterConfig(excluded_tags=["spam_spam", "abandoned"])
    allow, reason = should_process(_make_event(tags=["vip", "spam_spam"]), config)
    assert allow is False
    assert "spam_spam" in reason


def test_clean_tags_pass():
    config = FilterConfig(excluded_tags=["spam_spam", "abandoned"])
    allow, _ = should_process(_make_event(tags=["vip", "billing"]), config)
    assert allow is True


def test_multiple_excluded_tags_reported():
    config = FilterConfig(excluded_tags=["spam_spam", "abandoned"])
    allow, reason = should_process(
        _make_event(tags=["vip", "spam_spam", "abandoned"]), config
    )
    assert allow is False
    assert "spam_spam" in reason
    assert "abandoned" in reason


# --- combined ---


def test_both_filters_applied_group_blocks_first():
    config = FilterConfig(
        allowed_group_ids=["Support L1"], excluded_tags=["spam_spam"]
    )
    allow, reason = should_process(
        _make_event(group_id="Internal", tags=["spam_spam"]), config
    )
    assert allow is False
    # Group is checked first
    assert "allowed_group_ids" in reason


# --- tag parsing from string (Zendesk renders {{ticket.tags}} as a string) ---


def test_tags_parsed_from_space_separated_string():
    event = MessageEvent.model_validate(
        {
            "event_id": "evt-1",
            "event_timestamp": "2026-06-05T10:30:00Z",
            "ticket_id": "t1",
            "tags": "vip billing spam_spam",
            "messages": [
                {
                    "timestamp": "2026-06-05T10:30:00Z",
                    "author_type": "customer",
                    "body": "hi",
                }
            ],
        }
    )
    assert event.tags == ["vip", "billing", "spam_spam"]


def test_tags_parsed_from_comma_separated_string():
    event = MessageEvent.model_validate(
        {
            "event_id": "evt-1",
            "event_timestamp": "2026-06-05T10:30:00Z",
            "ticket_id": "t1",
            "tags": "vip, billing, spam_spam",
            "messages": [
                {
                    "timestamp": "2026-06-05T10:30:00Z",
                    "author_type": "customer",
                    "body": "hi",
                }
            ],
        }
    )
    assert event.tags == ["vip", "billing", "spam_spam"]


def test_tags_accepts_list_unchanged():
    event = MessageEvent.model_validate(
        {
            "event_id": "evt-1",
            "event_timestamp": "2026-06-05T10:30:00Z",
            "ticket_id": "t1",
            "tags": ["vip", "billing"],
            "messages": [
                {
                    "timestamp": "2026-06-05T10:30:00Z",
                    "author_type": "customer",
                    "body": "hi",
                }
            ],
        }
    )
    assert event.tags == ["vip", "billing"]


def test_empty_string_tags_becomes_empty_list():
    event = MessageEvent.model_validate(
        {
            "event_id": "evt-1",
            "event_timestamp": "2026-06-05T10:30:00Z",
            "ticket_id": "t1",
            "tags": "",
            "messages": [
                {
                    "timestamp": "2026-06-05T10:30:00Z",
                    "author_type": "customer",
                    "body": "hi",
                }
            ],
        }
    )
    assert event.tags == []


# --- config loading ---


def test_load_filter_config_from_disk():
    """The real filters.json should load and have the user's rules.

    Zendesk Event Subscriptions send group_id as the numeric Zendesk ID,
    so the config holds IDs not names.
    """
    config = load_filter_config()
    assert "44897999201817" in config.allowed_group_ids  # Support L1
    assert "6338786491161" in config.allowed_group_ids  # Support L2
    assert "spam_spam" in config.excluded_tags
    assert "abandoned" in config.excluded_tags
    assert "spam_abandoned" in config.excluded_tags
