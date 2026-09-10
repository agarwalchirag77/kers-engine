"""Deterministic commitment detection + scoring.

The AI was previously responsible for both detecting agent commitments and
deciding whether each one was honored. That conflated two jobs and led to a
real bug: commitments were marked MISSED while still inside their promised
window because the AI didn't know what "now" was.

This module owns the timing math. We detect commitments via patterns, compute
each deadline against `evaluation_time`, and classify the outcome as
DELIVERED, MISSED, or PENDING. The AI now receives the analysis as input and
uses it to inform other sub-scores (frustration, trajectory, etc.); the
`commitment_to_delivery_ratio` value is computed here.

Heuristic — we deliberately stay conservative:
  - Time RANGES use the upper bound ("10-15 min" → 15 min)
  - Grace period (default 2 min) before flipping PENDING → MISSED
  - Vague phrases ("shortly", "let me check") default to 15 min
  - When in doubt, default to PENDING (don't penalize the agent)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional

from .models import Message


GRACE_MINUTES = 0
"""How long past the deadline before we flip PENDING → MISSED. Zero means
a commitment becomes MISSED the instant the nominal deadline elapses."""

VAGUE_DEFAULT_MINUTES_CHAT = 60
"""Default deadline for vague phrases on chat/messaging tickets — these
channels expect rapid response, so 1 hour is the operating envelope."""

VAGUE_DEFAULT_MINUTES_EMAIL = 24 * 60
"""Default deadline for vague phrases on email and other async tickets —
SLAs for these channels measure in hours/days, so 24h is reasonable."""

CHAT_CHANNELS = frozenset({"native_messaging", "chat"})
"""Zendesk via.channel values we treat as 'chat' for SLA purposes."""


CommitmentStatus = Literal["DELIVERED", "MISSED", "PENDING"]


@dataclass
class Commitment:
    """A single detected commitment with its computed outcome."""

    phrase: str               # the substring that triggered detection
    made_at: datetime         # when the agent said it
    deadline: datetime        # when the promised follow-up was due
    status: CommitmentStatus
    fulfilled_at: Optional[datetime] = None
    """When the next agent message arrived — present iff DELIVERED."""


@dataclass
class CommitmentAnalysis:
    """Aggregated outcome of analysing one ticket's commitments."""

    commitments: list[Commitment] = field(default_factory=list)
    delivered: int = 0
    missed: int = 0
    pending: int = 0
    grace_minutes: int = GRACE_MINUTES
    evaluation_time: Optional[datetime] = None

    @property
    def detected(self) -> int:
        """Total commitments found (resolved + still-pending)."""
        return self.delivered + self.missed + self.pending

    @property
    def ratio_value(self) -> float:
        """commitment_to_delivery_ratio on the [0, 4] scale.

        Only resolved commitments (DELIVERED + MISSED) contribute. If every
        detected commitment is still PENDING, returns 0.0 — we have no
        evidence of poor delivery yet.
        """
        resolved = self.delivered + self.missed
        if resolved == 0:
            return 0.0
        return (self.missed / resolved) * 4.0

    def as_summary_lines(self) -> list[str]:
        """Compact human/AI-readable description used in the AI prompt."""
        lines = [
            f"detected:  {self.detected}",
            f"delivered: {self.delivered}",
            f"missed:    {self.missed}",
            f"pending:   {self.pending}",
            f"computed commitment_to_delivery_ratio: {self.ratio_value:.2f}",
        ]
        if self.commitments:
            lines.append("details:")
            for c in self.commitments:
                made = c.made_at.strftime("%Y-%m-%d %H:%M")
                deadl = c.deadline.strftime("%Y-%m-%d %H:%M")
                tail = ""
                if c.status == "DELIVERED" and c.fulfilled_at:
                    tail = f" (fulfilled at {c.fulfilled_at.strftime('%H:%M')})"
                lines.append(
                    f'  - "{c.phrase}" made at {made} → deadline {deadl} → {c.status}{tail}'
                )
        return lines


# ---------- detection patterns ----------

_TIME_UNIT = r"(?:minutes?|mins?|m\b|hours?|hrs?|h\b)"

# Range: "10-15 minutes", "5 to 10 minutes", "10–15 min" (en-dash too)
_RANGE_RE = re.compile(
    r"\b(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*" + _TIME_UNIT,
    re.IGNORECASE,
)

# Single duration: "10 minutes", "30 min", "1 hour", "2 hrs"
_SINGLE_RE = re.compile(r"\b(\d+)\s*" + _TIME_UNIT, re.IGNORECASE)

# Past-tense disqualifier markers that follow the duration
_PAST_TAIL_MARKERS = (
    " ago",
    " earlier",
    " before",
    " back",
    " prior",
)

# Vague commitment phrases — used only if no numeric match exists in the
# same message. 15-minute default deadline.
_VAGUE_PHRASES = (
    "shortly",
    "in a moment",
    "in a bit",
    "in a sec",
    "momentarily",
    "one moment",
    "be right back",
    "give me a moment",
    "give me a sec",
    "just a moment",
    "let me check",
    "let me look",
    "let me investigate",
    "let me get back",
    "looking into",
    "i'll get back",
    "ill get back",
    "i will get back",
    "i'll check",
    "ill check",
    "i will check",
)


_HOUR_UNIT_RE = re.compile(r"\b(?:hours?|hrs?|h)\b", re.IGNORECASE)


def _to_minutes(value: int, matched_text: str) -> int:
    """Detect whether the matched substring (e.g. '2 hours', '5 min') refers
    to hours or minutes. Looks for an hour-family token; defaults to minutes."""
    return value * 60 if _HOUR_UNIT_RE.search(matched_text) else value


def _has_past_tail(body_lower: str, end_pos: int) -> bool:
    """True if the match is followed by a past-tense marker like ' ago'."""
    tail = body_lower[end_pos : end_pos + 12]
    return any(tail.startswith(m) for m in _PAST_TAIL_MARKERS)


def _is_chat_channel(channel: str) -> bool:
    return (channel or "").lower() in CHAT_CHANNELS


def _vague_default_for_channel(channel: str) -> int:
    return (
        VAGUE_DEFAULT_MINUTES_CHAT
        if _is_chat_channel(channel)
        else VAGUE_DEFAULT_MINUTES_EMAIL
    )


def _extract_deadlines(
    body: str, made_at: datetime, vague_default_minutes: int
) -> list[tuple[str, datetime]]:
    """Find all commitment deadlines in one message body.

    Returns a list of (matched_substring, deadline_datetime). Multiple
    matches per body are possible (e.g., "I'll check in 5 min and call back
    within an hour").
    """
    if not body:
        return []
    text_lower = body.lower()
    found: list[tuple[str, datetime, int, int]] = []  # phrase, deadline, start, end
    used_spans: list[tuple[int, int]] = []

    # 1. Ranges first (so "10-15 minutes" is matched as range, not two singles)
    for m in _RANGE_RE.finditer(text_lower):
        if _has_past_tail(text_lower, m.end()):
            continue
        upper = int(m.group(2))
        unit_text = m.group(0)
        minutes = _to_minutes(upper, unit_text)
        found.append(
            (m.group(0), made_at + timedelta(minutes=minutes), m.start(), m.end())
        )
        used_spans.append((m.start(), m.end()))

    # 2. Singles, skipping anything inside a range we already consumed
    for m in _SINGLE_RE.finditer(text_lower):
        if any(start <= m.start() < end for start, end in used_spans):
            continue
        if _has_past_tail(text_lower, m.end()):
            continue
        n = int(m.group(1))
        unit_text = m.group(0)
        minutes = _to_minutes(n, unit_text)
        # Sanity: ignore "0 minutes" and absurd numbers (> 24 hours)
        if minutes <= 0 or minutes > 24 * 60:
            continue
        found.append(
            (m.group(0), made_at + timedelta(minutes=minutes), m.start(), m.end())
        )

    # 3. Vague phrases — only if we found nothing numeric.
    # Deadline depends on channel: chat = 1h, email/other = 24h.
    if not found:
        for phrase in _VAGUE_PHRASES:
            idx = text_lower.find(phrase)
            if idx >= 0:
                found.append(
                    (
                        phrase,
                        made_at + timedelta(minutes=vague_default_minutes),
                        idx,
                        idx + len(phrase),
                    )
                )
                break  # one vague match per message is enough

    return [(phrase, deadline) for phrase, deadline, _, _ in found]


def _next_agent_message_after(
    messages: list[Message], from_index: int
) -> Optional[Message]:
    """First agent message strictly after `from_index`. None if none."""
    for m in messages[from_index + 1 :]:
        if m.author_type == "agent":
            return m
    return None


# ---------- main API ----------


def analyze_commitments(
    messages: list[Message],
    evaluation_time: datetime,
    *,
    channel: str = "",
    grace_minutes: int = GRACE_MINUTES,
) -> CommitmentAnalysis:
    """Detect and classify every commitment the agent made.

    Args:
        messages: full conversation, chronological. (Customer + agent.)
        evaluation_time: timezone-aware "now" — typically datetime.now(UTC)
            at the moment the worker decides to evaluate. Used to decide
            whether deadlines have actually passed.
        channel: Zendesk via.channel value, controls the vague-phrase
            default. "native_messaging"/"chat" → 1h. Anything else → 24h.
        grace_minutes: how long past a deadline before MISSED. Default 0.

    Returns:
        CommitmentAnalysis with detected commitments, counts, and the
        computed ratio.
    """
    grace = timedelta(minutes=grace_minutes)
    vague_default = _vague_default_for_channel(channel)
    commitments: list[Commitment] = []

    for i, msg in enumerate(messages):
        if msg.author_type != "agent":
            continue
        for phrase, deadline in _extract_deadlines(msg.body, msg.timestamp, vague_default):
            next_agent = _next_agent_message_after(messages, i)
            status: CommitmentStatus
            fulfilled_at: Optional[datetime] = None

            if next_agent is not None and next_agent.timestamp <= deadline + grace:
                status = "DELIVERED"
                fulfilled_at = next_agent.timestamp
            elif evaluation_time > deadline + grace:
                status = "MISSED"
            else:
                status = "PENDING"

            commitments.append(
                Commitment(
                    phrase=phrase,
                    made_at=msg.timestamp,
                    deadline=deadline,
                    status=status,
                    fulfilled_at=fulfilled_at,
                )
            )

    return CommitmentAnalysis(
        commitments=commitments,
        delivered=sum(1 for c in commitments if c.status == "DELIVERED"),
        missed=sum(1 for c in commitments if c.status == "MISSED"),
        pending=sum(1 for c in commitments if c.status == "PENDING"),
        grace_minutes=grace_minutes,
        evaluation_time=evaluation_time,
    )
