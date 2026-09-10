from __future__ import annotations

import asyncio
import time
from typing import Optional

from .ai_client import AIClientError
from .ers import compute_ers
from .interfaces import AIClient, TicketStore, ZendeskPusher
from .models import ClosedEvent, MessageEvent, WeightConfig
from .storage.db import Database
from .storage.logger import StructuredLogger
from .storage.ticket_files import TicketNotFoundError


class Worker:
    """Owns the async event loop. Consumes from a queue, debounces per-ticket
    evaluations, runs the AI call, computes ERS, pushes to Zendesk, persists."""

    def __init__(
        self,
        *,
        ticket_store: TicketStore,
        ai_client: AIClient,
        zd_pusher: ZendeskPusher,
        db: Database,
        logger: StructuredLogger,
        weight_config: WeightConfig,
        model_version: str,
        prompt_version: str,
    ):
        self.ticket_store = ticket_store
        self.ai_client = ai_client
        self.zd_pusher = zd_pusher
        self.db = db
        self.logger = logger
        self.weight_config = weight_config
        self.model_version = model_version
        self.prompt_version = prompt_version

        self.queue: asyncio.Queue = asyncio.Queue()
        self._ticket_locks: dict[str, asyncio.Lock] = {}
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._seen_events: set[str] = set()
        self._loop_task: Optional[asyncio.Task] = None

    def _lock_for(self, ticket_id: str) -> asyncio.Lock:
        lock = self._ticket_locks.get(ticket_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ticket_locks[ticket_id] = lock
        return lock

    async def enqueue_message(self, event: MessageEvent) -> None:
        await self.queue.put(("message", event))

    async def enqueue_close(self, event: ClosedEvent) -> None:
        await self.queue.put(("close", event))

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for task in list(self._debounce_tasks.values()):
            if not task.done():
                task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                kind, event = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                if kind == "message":
                    await self._handle_message(event)
                elif kind == "close":
                    await self._handle_close(event)
            except Exception as e:
                self.logger.error(
                    stage="worker_exception",
                    ticket_id=getattr(event, "ticket_id", None),
                    error=str(e),
                )

    async def _handle_message(self, event: MessageEvent) -> None:
        if event.event_id in self._seen_events:
            self.logger.info(
                stage="duplicate_event_ignored",
                ticket_id=event.ticket_id,
                extra={"event_id": event.event_id},
            )
            return
        self._seen_events.add(event.event_id)

        ticket_id = event.ticket_id
        async with self._lock_for(ticket_id):
            self.ticket_store.get_or_create(ticket_id, event.metadata())
            for msg in event.messages:
                self.ticket_store.append_message(ticket_id, msg)
        self.logger.info(
            stage="message_appended",
            ticket_id=ticket_id,
            extra={"new_messages": len(event.messages)},
        )

        existing = self._debounce_tasks.get(ticket_id)
        if existing and not existing.done():
            existing.cancel()
        self._debounce_tasks[ticket_id] = asyncio.create_task(
            self._debounced_evaluate(ticket_id, self.weight_config.debounce_seconds)
        )
        self.logger.info(
            stage="debounce_scheduled",
            ticket_id=ticket_id,
            extra={"seconds": self.weight_config.debounce_seconds},
        )

    async def _debounced_evaluate(self, ticket_id: str, delay_seconds: int) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock_for(ticket_id):
            await self._evaluate(ticket_id)

    async def _evaluate(self, ticket_id: str) -> None:
        msg_count = self.ticket_store.get_message_count(ticket_id)
        if msg_count < self.weight_config.min_messages_before_eval:
            self.logger.info(
                stage="eval_skipped_under_threshold",
                ticket_id=ticket_id,
                extra={
                    "message_count": msg_count,
                    "min": self.weight_config.min_messages_before_eval,
                },
            )
            return

        try:
            metadata = self.ticket_store.get_metadata(ticket_id)
            context = self.ticket_store.get_context_for_ai(ticket_id)
        except TicketNotFoundError as e:
            self.logger.error(
                stage="eval_skipped_ticket_missing",
                ticket_id=ticket_id,
                error=str(e),
            )
            return

        self.logger.info(
            stage="ai_called",
            ticket_id=ticket_id,
            extra={"message_count": context.message_count, "model": self.model_version},
        )
        start = time.monotonic()
        try:
            sentiment = await self.ai_client.score(context, metadata)
        except AIClientError as e:
            self.logger.error(
                stage="ai_response_invalid",
                ticket_id=ticket_id,
                error=str(e),
                duration_ms=(time.monotonic() - start) * 1000,
            )
            return
        ai_duration_ms = (time.monotonic() - start) * 1000
        self.logger.info(
            stage="ai_response_received",
            ticket_id=ticket_id,
            duration_ms=ai_duration_ms,
            extra={
                "top_signal": sentiment.top_signal,
                "confidence": sentiment.confidence,
            },
        )

        ers = compute_ers(sentiment.breakdown, self.weight_config.weights)
        self.logger.info(
            stage="ers_computed",
            ticket_id=ticket_id,
            extra={"ers": ers, "threshold": self.weight_config.threshold},
        )

        push_result = await self.zd_pusher.push_ers(ticket_id, ers)
        if push_result.success:
            self.logger.info(
                stage="pushed_to_zd", ticket_id=ticket_id, extra={"ers": ers}
            )
        else:
            self.logger.error(
                stage="push_failed", ticket_id=ticket_id, error=push_result.error
            )

        try:
            self.db.insert_ers_event(
                ticket_id=ticket_id,
                ers=ers,
                sentiment=sentiment,
                message_count=context.message_count,
                threshold=self.weight_config.threshold,
                push_result=push_result,
                metadata=metadata,
                model_version=self.model_version,
                prompt_version=self.prompt_version,
            )
            self.logger.info(stage="db_written", ticket_id=ticket_id)
        except Exception as e:
            self.logger.error(
                stage="db_write_failed", ticket_id=ticket_id, error=str(e)
            )

    async def _handle_close(self, event: ClosedEvent) -> None:
        ticket_id = event.ticket_id
        pending = self._debounce_tasks.pop(ticket_id, None)
        if pending and not pending.done():
            pending.cancel()

        async with self._lock_for(ticket_id):
            self.ticket_store.purge(ticket_id)
            updated = self.db.update_resolution(ticket_id, event.resolution_outcome)

        self.logger.info(
            stage="ticket_purged",
            ticket_id=ticket_id,
            extra={"rows_updated": updated, "outcome": event.resolution_outcome},
        )
