from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.models import (
    ClosedEvent,
    Message,
    MessageEvent,
    WeightConfig,
)
from app.storage.db import Database
from app.storage.logger import StructuredLogger
from app.storage.ticket_files import JsonTicketStore
from app.worker import Worker

from .conftest import FakeAIClient, FakeZendeskPusher, make_sentiment


def make_event(ticket_id: str, event_id: str, msg_count: int = 1) -> MessageEvent:
    msgs = [
        Message(
            timestamp=datetime(2026, 6, 1, 10, i, tzinfo=timezone.utc),
            author_type="customer" if i % 2 == 0 else "agent",
            body=f"msg-{i}",
        )
        for i in range(msg_count)
    ]
    return MessageEvent(
        event_id=event_id,
        event_timestamp=datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc),
        ticket_id=ticket_id,
        subject="Test",
        group_id="g1",
        priority="normal",
        channel="email",
        tags=[],
        locale="en-US",
        requester_id_hash="abc",
        agent_email="agent@x.com",
        messages=msgs,
    )


@pytest.fixture
def fast_weight_config(default_weight_config) -> WeightConfig:
    """Same as default but with a tiny debounce so tests are fast."""
    return default_weight_config.model_copy(update={"debounce_seconds": 0})


@pytest.fixture
def build_worker(tmp_path, fast_weight_config):
    def _build(ai_client=None, zd_pusher=None):
        ai = ai_client or FakeAIClient(make_sentiment(ers_target=3.5))
        zd = zd_pusher or FakeZendeskPusher(success=True)
        worker = Worker(
            ticket_store=JsonTicketStore(tmp_path / "tickets"),
            ai_client=ai,
            zd_pusher=zd,
            db=Database(tmp_path / "data" / "test.sqlite"),
            logger=StructuredLogger(tmp_path / "logs"),
            weight_config=fast_weight_config,
            model_version="fake-model",
            prompt_version="ers_prompt_v1",
        )
        return worker, ai, zd
    return _build


async def _drain(worker: Worker, timeout: float = 1.0):
    """Wait until the worker's queue is empty and any pending debounce
    tasks have completed."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
        if worker.queue.empty() and all(
            t.done() for t in worker._debounce_tasks.values()
        ):
            return
    raise AssertionError("worker did not drain in time")


async def test_eval_skipped_under_message_threshold(build_worker):
    worker, ai, zd = build_worker()
    worker.start()
    try:
        # Send 5 messages — should not trigger evaluation
        event = make_event("t1", "evt-1", msg_count=5)
        await worker.enqueue_message(event)
        await _drain(worker)
        assert ai.calls == [], "AI should not be called with < 6 messages"
        assert zd.pushes == [], "No push expected"
    finally:
        await worker.stop()


async def test_eval_runs_at_threshold(build_worker):
    worker, ai, zd = build_worker()
    worker.start()
    try:
        event = make_event("t1", "evt-1", msg_count=6)
        await worker.enqueue_message(event)
        await _drain(worker)
        assert len(ai.calls) == 1
        assert len(zd.pushes) == 1
        ticket_id, ers = zd.pushes[0]
        assert ticket_id == "t1"
        assert ers == pytest.approx(3.5)
    finally:
        await worker.stop()


async def test_dedup_by_event_id(build_worker):
    worker, ai, zd = build_worker()
    worker.start()
    try:
        event = make_event("t1", "evt-1", msg_count=6)
        await worker.enqueue_message(event)
        await worker.enqueue_message(event)  # same event_id
        await _drain(worker)
        # Only one AI call despite two webhooks with same event_id
        assert len(ai.calls) == 1
    finally:
        await worker.stop()


async def test_debounce_coalesces_burst(build_worker, fast_weight_config):
    """A burst of 3 messages within the debounce window should produce one AI call."""
    # Use a slightly longer debounce so we can interleave events
    config = fast_weight_config.model_copy(update={"debounce_seconds": 1})
    worker, ai, zd = build_worker()
    worker.weight_config = config
    worker.start()
    try:
        # First event: 3 messages (still under threshold)
        await worker.enqueue_message(make_event("t1", "evt-1", msg_count=3))
        await asyncio.sleep(0.1)
        # Second event: 3 more messages, total 6 — would trigger but debounce resets
        await worker.enqueue_message(make_event("t1", "evt-2", msg_count=3))
        await asyncio.sleep(0.1)
        # Third event: 1 more message
        await worker.enqueue_message(make_event("t1", "evt-3", msg_count=1))
        # Wait past the debounce window
        await asyncio.sleep(1.5)
        await _drain(worker, timeout=2.0)
        # Only one evaluation should have happened (debounce coalesced them)
        assert len(ai.calls) == 1
    finally:
        await worker.stop()


async def test_close_purges_ticket_and_updates_db(build_worker, tmp_path):
    worker, ai, zd = build_worker()
    worker.start()
    try:
        # Run an evaluation first so there's a DB row
        await worker.enqueue_message(make_event("t1", "evt-1", msg_count=6))
        await _drain(worker)
        ticket_file = tmp_path / "tickets" / "t1.json"
        assert ticket_file.exists()

        # Now close the ticket
        close = ClosedEvent(
            event_id="close-1",
            event_timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc),
            ticket_id="t1",
            resolution_outcome="solved",
        )
        await worker.enqueue_close(close)
        await _drain(worker)

        assert not ticket_file.exists(), "ticket file should be purged on close"
        # DB row should still be there but with resolution backfilled
        with worker.db._connect() as conn:
            row = conn.execute(
                "SELECT resolution_outcome FROM ers_events WHERE ticket_id = 't1'"
            ).fetchone()
        assert row["resolution_outcome"] == "solved"
    finally:
        await worker.stop()


async def test_push_failure_still_persists_row(build_worker):
    worker, ai, zd = build_worker(zd_pusher=FakeZendeskPusher(success=False))
    worker.start()
    try:
        await worker.enqueue_message(make_event("t1", "evt-1", msg_count=6))
        await _drain(worker)
        # DB row should still be inserted even though push failed
        with worker.db._connect() as conn:
            row = conn.execute(
                "SELECT pushed_to_zd, push_error FROM ers_events WHERE ticket_id = 't1'"
            ).fetchone()
        assert row["pushed_to_zd"] == 0
        assert row["push_error"] is not None
    finally:
        await worker.stop()


async def test_ai_failure_skips_db_row(build_worker):
    """EH1/EH2: if the AI call fails after retry, no DB row is written."""
    from app.ai_client import AIClientError

    class FailingAI:
        model = "fake-model"
        prompt_version = "ers_prompt_v1"

        async def score(self, context, metadata):
            raise AIClientError("simulated")

    worker, ai, zd = build_worker(ai_client=FailingAI())
    worker.start()
    try:
        await worker.enqueue_message(make_event("t1", "evt-1", msg_count=6))
        await _drain(worker)
        with worker.db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ers_events WHERE ticket_id = 't1'"
            ).fetchone()
        assert row["n"] == 0
        assert zd.pushes == []
    finally:
        await worker.stop()
