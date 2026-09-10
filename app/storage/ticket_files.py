from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import ConversationContext, Message, TicketFile, TicketMetadata


class TicketNotFoundError(Exception):
    pass


class JsonTicketStore:
    """v1 TicketStore: one JSON file per ticket, full history kept.

    Future implementations (summary + last-K, S3 backend, DB-backed BLOBs) can
    expose the same methods without touching the worker or AI client.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ticket_id: str) -> Path:
        return self.base_dir / f"{ticket_id}.json"

    def _load(self, ticket_id: str) -> Optional[TicketFile]:
        path = self._path(ticket_id)
        if not path.exists():
            return None
        try:
            with path.open() as f:
                data = json.load(f)
            return TicketFile.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            # EH11: corrupted ticket file — move aside, start fresh
            corrupted = path.with_suffix(".corrupted")
            path.rename(corrupted)
            return None

    def _save(self, tf: TicketFile) -> None:
        path = self._path(tf.ticket_id)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(tf.model_dump(mode="json"), f, indent=2, default=str)
        tmp.replace(path)

    def get_or_create(self, ticket_id: str, metadata: TicketMetadata) -> TicketFile:
        tf = self._load(ticket_id)
        if tf is not None:
            return tf
        now = datetime.now(timezone.utc)
        tf = TicketFile(
            ticket_id=metadata.ticket_id,
            subject=metadata.subject,
            group_id=metadata.group_id,
            priority=metadata.priority,
            channel=metadata.channel,
            tags=metadata.tags,
            locale=metadata.locale,
            requester_id_hash=metadata.requester_id_hash,
            agent_email=metadata.agent_email,
            created_at=now,
            last_updated_at=now,
            messages=[],
        )
        self._save(tf)
        return tf

    def upsert_metadata(self, ticket_id: str, metadata: TicketMetadata) -> TicketFile:
        """Create the ticket file if missing, or refresh metadata fields if it
        exists (preserving any messages already accumulated).

        Used to cache ticket-level metadata from ticket.* events so that
        skinny messaging_ticket.* events can later be enriched with group/tags
        without an extra Zendesk API round-trip.

        A non-empty value in `metadata` overwrites the stored value; an empty
        string / empty list leaves the stored value intact.
        """
        tf = self._load(ticket_id)
        now = datetime.now(timezone.utc)
        if tf is None:
            tf = TicketFile(
                ticket_id=metadata.ticket_id,
                subject=metadata.subject,
                group_id=metadata.group_id,
                priority=metadata.priority,
                channel=metadata.channel,
                tags=metadata.tags,
                locale=metadata.locale,
                requester_id_hash=metadata.requester_id_hash,
                agent_email=metadata.agent_email,
                created_at=now,
                last_updated_at=now,
                messages=[],
            )
        else:
            if metadata.subject:           tf.subject = metadata.subject
            if metadata.group_id:          tf.group_id = metadata.group_id
            if metadata.priority:          tf.priority = metadata.priority
            if metadata.channel:           tf.channel = metadata.channel
            if metadata.tags:              tf.tags = metadata.tags
            if metadata.locale:            tf.locale = metadata.locale
            if metadata.requester_id_hash: tf.requester_id_hash = metadata.requester_id_hash
            if metadata.agent_email:       tf.agent_email = metadata.agent_email
            tf.last_updated_at = now
        self._save(tf)
        return tf

    def append_message(self, ticket_id: str, msg: Message) -> None:
        tf = self._load(ticket_id)
        if tf is None:
            raise TicketNotFoundError(
                f"Ticket {ticket_id} not found; call get_or_create first"
            )
        tf.messages.append(msg)
        # EH15: keep messages chronologically ordered even if a webhook arrives
        # with a timestamp earlier than the last stored one.
        tf.messages.sort(key=lambda m: m.timestamp)
        tf.last_updated_at = datetime.now(timezone.utc)
        self._save(tf)

    def get_message_count(self, ticket_id: str) -> int:
        tf = self._load(ticket_id)
        return 0 if tf is None else len(tf.messages)

    def get_metadata(self, ticket_id: str) -> TicketMetadata:
        tf = self._load(ticket_id)
        if tf is None:
            raise TicketNotFoundError(f"Ticket {ticket_id} not found")
        return tf.metadata()

    def get_context_for_ai(self, ticket_id: str) -> ConversationContext:
        tf = self._load(ticket_id)
        if tf is None or not tf.messages:
            raise TicketNotFoundError(
                f"Ticket {ticket_id} has no stored conversation"
            )
        return ConversationContext(
            formatted_messages=self.format_messages(tf.messages),
            message_count=len(tf.messages),
            messages=list(tf.messages),
        )

    @staticmethod
    def format_messages(messages: list[Message]) -> str:
        blocks = []
        for m in messages:
            ts = m.timestamp.strftime("%Y-%m-%d %H:%M")
            blocks.append(f"[{ts} | {m.author_type}]\n{m.body}")
        return "\n\n".join(blocks)

    def purge(self, ticket_id: str) -> None:
        path = self._path(ticket_id)
        if path.exists():
            path.unlink()
