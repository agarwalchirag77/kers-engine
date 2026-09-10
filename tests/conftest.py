from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make `app` importable from the engine root regardless of where pytest is run.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.models import (  # noqa: E402
    ConversationContext,
    Message,
    PushResult,
    SentimentResponse,
    TicketMetadata,
    WeightConfig,
)


@pytest.fixture
def default_weight_config() -> WeightConfig:
    return WeightConfig(
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
        debounce_seconds=60,
        min_messages_before_eval=6,
    )


@pytest.fixture
def sample_metadata() -> TicketMetadata:
    return TicketMetadata(
        ticket_id="12345",
        subject="Billing dispute",
        group_id="support-billing",
        channel="email",
        priority="normal",
        tags=["billing", "vip"],
        locale="en-US",
        requester_id_hash="a3f9e1",
        agent_email="jane@example.com",
    )


def make_message(ts: str, author: str, body: str) -> Message:
    return Message(
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        author_type=author,  # type: ignore[arg-type]
        body=body,
    )


@pytest.fixture
def six_messages() -> list[Message]:
    base = "2026-06-01T10:0{}:00"
    return [
        make_message(base.format(i), "customer" if i % 2 == 0 else "agent", f"msg {i}")
        for i in range(6)
    ]


def make_sentiment(
    *,
    ers_target: float = 3.5,
    commitments_missed: int = 0,
    commitments_detected: int = 0,
    confidence: float = 4.0,
    top_signal: str = "churn_threat",
) -> SentimentResponse:
    """Build a valid SentimentResponse. ers_target is hit by setting all
    sub-scores to ers_target (since weights sum to 1)."""
    return SentimentResponse(
        frustration=ers_target,
        urgency=ers_target,
        churn_threat=ers_target,
        confusion=ers_target,
        politeness_erosion=ers_target,
        dissatisfaction_trajectory=ers_target,
        agent_tone=ers_target,
        commitment_to_delivery_ratio=ers_target,
        commitments_detected=commitments_detected,
        commitments_missed=commitments_missed,
        top_signal=top_signal,
        confidence=confidence,
        reasoning="test reasoning",
    )


class FakeAIClient:
    """In-memory AIClient that returns a canned response."""

    def __init__(self, sentiment: SentimentResponse, model: str = "fake-model"):
        self.sentiment = sentiment
        self.model = model
        self.prompt_version = "ers_prompt_v1"
        self.calls: list[tuple[ConversationContext, TicketMetadata]] = []

    async def score(self, context, metadata) -> SentimentResponse:
        self.calls.append((context, metadata))
        return self.sentiment


class FakeZendeskPusher:
    """In-memory ZendeskPusher that records pushes."""

    def __init__(self, success: bool = True):
        self.success = success
        self.pushes: list[tuple[str, float]] = []

    async def push_ers(self, ticket_id: str, ers: float) -> PushResult:
        self.pushes.append((ticket_id, ers))
        if self.success:
            return PushResult(success=True, pushed_at=datetime.now(timezone.utc))
        return PushResult(success=False, error="fake failure")

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_ai_factory():
    def _make(sentiment: SentimentResponse = None) -> FakeAIClient:
        return FakeAIClient(sentiment or make_sentiment())
    return _make


@pytest.fixture
def fake_zd_factory():
    def _make(success: bool = True) -> FakeZendeskPusher:
        return FakeZendeskPusher(success=success)
    return _make
