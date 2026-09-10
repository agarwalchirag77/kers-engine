from __future__ import annotations

from typing import Protocol

from .models import (
    ConversationContext,
    Message,
    PushResult,
    SentimentResponse,
    TicketFile,
    TicketMetadata,
)


class TicketStore(Protocol):
    """File-backed conversation store. v1 returns full history; v2 implementations
    can return summary + last-K or windowed slices without caller changes."""

    def get_or_create(self, ticket_id: str, metadata: TicketMetadata) -> TicketFile: ...

    def append_message(self, ticket_id: str, msg: Message) -> None: ...

    def get_message_count(self, ticket_id: str) -> int: ...

    def get_metadata(self, ticket_id: str) -> TicketMetadata: ...

    def get_context_for_ai(self, ticket_id: str) -> ConversationContext: ...

    def purge(self, ticket_id: str) -> None: ...


class AIClient(Protocol):
    """Wraps an LLM provider. Prompt caching, retries, model selection, response
    validation all live behind this interface."""

    async def score(
        self, context: ConversationContext, metadata: TicketMetadata
    ) -> SentimentResponse: ...


class ZendeskPusher(Protocol):
    """Writes the computed ERS to a Zendesk custom field."""

    async def push_ers(self, ticket_id: str, ers: float) -> PushResult: ...
