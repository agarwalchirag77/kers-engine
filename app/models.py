from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


AuthorType = Literal["customer", "agent"]


SUB_SCORE_NAMES = (
    "frustration",
    "urgency",
    "churn_threat",
    "confusion",
    "politeness_erosion",
    "dissatisfaction_trajectory",
    "agent_tone",
    "commitment_to_delivery_ratio",
)


class Message(BaseModel):
    timestamp: datetime
    author_type: AuthorType
    body: str
    author_id: str = ""
    """Platform-specific id of whoever wrote this message (Zendesk user id, or
    a messaging actor id). Optional and defaults to empty so ticket files
    written before this field existed still parse.

    `author_type` says customer-or-agent; this says WHICH one. Needed to
    attribute an ERS movement to a specific agent — see
    scripts/find_saves.py."""


class TicketMetadata(BaseModel):
    ticket_id: str
    subject: str = ""
    group_id: str = ""
    priority: str = ""
    channel: str = ""
    tags: list[str] = Field(default_factory=list)
    locale: str = "en-US"
    requester_id_hash: str = ""
    agent_email: str = ""


class TicketFile(BaseModel):
    ticket_id: str
    subject: str = ""
    group_id: str = ""
    priority: str = ""
    channel: str = ""
    tags: list[str] = Field(default_factory=list)
    locale: str = "en-US"
    requester_id_hash: str = ""
    agent_email: str = ""
    created_at: datetime
    last_updated_at: datetime
    messages: list[Message] = Field(default_factory=list)

    def metadata(self) -> TicketMetadata:
        return TicketMetadata(
            ticket_id=self.ticket_id,
            subject=self.subject,
            group_id=self.group_id,
            priority=self.priority,
            channel=self.channel,
            tags=self.tags,
            locale=self.locale,
            requester_id_hash=self.requester_id_hash,
            agent_email=self.agent_email,
        )


class MessageEvent(BaseModel):
    """Inbound webhook payload for a new public message."""

    event_id: str
    event_timestamp: datetime
    ticket_id: str
    subject: str = ""
    group_id: str = ""
    priority: str = ""
    channel: str = ""
    tags: list[str] = Field(default_factory=list)
    locale: str = "en-US"
    requester_id_hash: str = ""
    agent_email: str = ""
    messages: list[Message]

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags(cls, v):
        """Zendesk renders {{ticket.tags}} as a space-separated string.
        Accept either a list or a space/comma-separated string."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.replace(",", " ").split() if t.strip()]
        return v

    def metadata(self) -> TicketMetadata:
        return TicketMetadata(
            ticket_id=self.ticket_id,
            subject=self.subject,
            group_id=self.group_id,
            priority=self.priority,
            channel=self.channel,
            tags=self.tags,
            locale=self.locale,
            requester_id_hash=self.requester_id_hash,
            agent_email=self.agent_email,
        )


class ClosedEvent(BaseModel):
    """Inbound webhook payload for a ticket.closed event."""

    event_id: str
    event_timestamp: datetime
    ticket_id: str
    resolution_outcome: Optional[str] = None


class ConversationContext(BaseModel):
    """What gets sent to the AI. The format is opaque to callers — TicketStore
    decides how to construct it (full history in v1; summary + recent in v2)."""

    formatted_messages: str
    message_count: int
    messages: list[Message] = Field(default_factory=list)
    """Raw message list. Used by commitment analysis (which needs timestamps
    and author_type, not the rendered text). The rendered string above is
    what the AI actually sees in its prompt."""


class SentimentBreakdown(BaseModel):
    frustration: float = Field(ge=0.0, le=4.0)
    urgency: float = Field(ge=0.0, le=4.0)
    churn_threat: float = Field(ge=0.0, le=4.0)
    confusion: float = Field(ge=0.0, le=4.0)
    politeness_erosion: float = Field(ge=0.0, le=4.0)
    dissatisfaction_trajectory: float = Field(ge=0.0, le=4.0)
    agent_tone: float = Field(ge=0.0, le=4.0)
    commitment_to_delivery_ratio: float = Field(ge=0.0, le=4.0)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class SentimentResponse(BaseModel):
    """Validated AI response."""

    frustration: float = Field(ge=0.0, le=4.0)
    urgency: float = Field(ge=0.0, le=4.0)
    churn_threat: float = Field(ge=0.0, le=4.0)
    confusion: float = Field(ge=0.0, le=4.0)
    politeness_erosion: float = Field(ge=0.0, le=4.0)
    dissatisfaction_trajectory: float = Field(ge=0.0, le=4.0)
    agent_tone: float = Field(ge=0.0, le=4.0)
    commitment_to_delivery_ratio: float = Field(ge=0.0, le=4.0)
    commitments_detected: int = Field(ge=0)
    commitments_missed: int = Field(ge=0)
    top_signal: str
    confidence: float = Field(ge=0.0, le=4.0)
    reasoning: str = Field(min_length=1, max_length=1200)
    """Cap raised from 500 for prompt v5, which asks the model to name the
    specific language behind each high metric — that produces longer text.
    With temperature=0 and a fixed seed the retry regenerates byte-identical
    output, so an over-length reasoning is a PERMANENT failure, not a
    transient one: too tight a cap silently drops the whole evaluation."""

    @field_validator("top_signal")
    @classmethod
    def top_signal_must_be_known(cls, v: str) -> str:
        if v not in SUB_SCORE_NAMES:
            raise ValueError(f"top_signal must be one of {SUB_SCORE_NAMES}, got {v!r}")
        return v

    @field_validator("commitments_missed")
    @classmethod
    def missed_le_detected(cls, v: int, info) -> int:
        detected = info.data.get("commitments_detected")
        if detected is not None and v > detected:
            raise ValueError(
                f"commitments_missed ({v}) cannot exceed commitments_detected ({detected})"
            )
        return v

    @property
    def breakdown(self) -> SentimentBreakdown:
        return SentimentBreakdown(
            frustration=self.frustration,
            urgency=self.urgency,
            churn_threat=self.churn_threat,
            confusion=self.confusion,
            politeness_erosion=self.politeness_erosion,
            dissatisfaction_trajectory=self.dissatisfaction_trajectory,
            agent_tone=self.agent_tone,
            commitment_to_delivery_ratio=self.commitment_to_delivery_ratio,
        )


class WeightConfig(BaseModel):
    weights: dict[str, float]
    threshold: float
    debounce_seconds: int
    min_messages_before_eval: int


class PushResult(BaseModel):
    success: bool
    pushed_at: Optional[datetime] = None
    error: Optional[str] = None
