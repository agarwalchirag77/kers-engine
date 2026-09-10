from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Message
from app.storage.ticket_files import JsonTicketStore, TicketNotFoundError


def test_get_or_create_creates_new(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    tf = store.get_or_create("12345", sample_metadata)
    assert tf.ticket_id == "12345"
    assert tf.messages == []
    assert (tmp_path / "12345.json").exists()


def test_get_or_create_returns_existing(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    msg = Message(
        timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        author_type="customer",
        body="hello",
    )
    store.append_message("12345", msg)
    tf2 = store.get_or_create("12345", sample_metadata)
    assert len(tf2.messages) == 1


def test_append_increments_count(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    assert store.get_message_count("12345") == 0
    store.append_message(
        "12345",
        Message(
            timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            author_type="customer",
            body="hi",
        ),
    )
    assert store.get_message_count("12345") == 1


def test_append_out_of_order_sorts_chronologically(tmp_path, sample_metadata):
    """EH15: a message with an earlier timestamp should be inserted in order."""
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    later = Message(
        timestamp=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        author_type="customer",
        body="later",
    )
    earlier = Message(
        timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        author_type="customer",
        body="earlier",
    )
    store.append_message("12345", later)
    store.append_message("12345", earlier)
    ctx = store.get_context_for_ai("12345")
    # "earlier" should appear before "later" in the formatted string
    assert ctx.formatted_messages.index("earlier") < ctx.formatted_messages.index("later")


def test_get_context_for_ai_formats_correctly(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    store.append_message(
        "12345",
        Message(
            timestamp=datetime(2026, 6, 1, 10, 4, tzinfo=timezone.utc),
            author_type="agent",
            body="give me 10 minutes",
        ),
    )
    ctx = store.get_context_for_ai("12345")
    assert "[2026-06-01 10:04 | agent]" in ctx.formatted_messages
    assert "give me 10 minutes" in ctx.formatted_messages
    assert ctx.message_count == 1


def test_purge_removes_file(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    assert (tmp_path / "12345.json").exists()
    store.purge("12345")
    assert not (tmp_path / "12345.json").exists()


def test_get_metadata_raises_when_missing(tmp_path):
    store = JsonTicketStore(tmp_path)
    with pytest.raises(TicketNotFoundError):
        store.get_metadata("does-not-exist")


def test_upsert_metadata_creates_when_missing(tmp_path, sample_metadata):
    store = JsonTicketStore(tmp_path)
    tf = store.upsert_metadata("new-ticket", sample_metadata)
    assert tf.ticket_id == "12345"
    assert tf.group_id == sample_metadata.group_id
    assert tf.messages == []


def test_upsert_metadata_updates_existing_preserving_messages(tmp_path, sample_metadata):
    from app.models import TicketMetadata
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    store.append_message(
        "12345",
        Message(
            timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            author_type="customer",
            body="hi",
        ),
    )
    new_md = TicketMetadata(
        ticket_id="12345",
        subject="Updated subject",
        group_id="new-group",
        tags=["new-tag"],
    )
    store.upsert_metadata("12345", new_md)
    tf = store._load("12345")
    assert tf.subject == "Updated subject"
    assert tf.group_id == "new-group"
    assert tf.tags == ["new-tag"]
    assert len(tf.messages) == 1
    assert tf.messages[0].body == "hi"


def test_upsert_metadata_empty_values_dont_overwrite(tmp_path, sample_metadata):
    """Empty values in the new metadata leave existing values intact."""
    from app.models import TicketMetadata
    store = JsonTicketStore(tmp_path)
    store.get_or_create("12345", sample_metadata)
    empty_md = TicketMetadata(ticket_id="12345")  # all defaults = empty
    store.upsert_metadata("12345", empty_md)
    tf = store._load("12345")
    assert tf.subject == sample_metadata.subject
    assert tf.group_id == sample_metadata.group_id
    assert tf.tags == sample_metadata.tags


def test_corrupted_file_is_moved_aside(tmp_path, sample_metadata):
    """EH11: a corrupted JSON file should be moved aside, fresh state created."""
    store = JsonTicketStore(tmp_path)
    path = tmp_path / "12345.json"
    path.write_text("{not valid json")
    # _load should return None after moving the corrupted file
    assert store._load("12345") is None
    assert (tmp_path / "12345.corrupted").exists()
