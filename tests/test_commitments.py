"""Tests for the deterministic commitment detector + scorer.

We're aiming for confidence on three layers:
  1. Pattern detection (which phrases trigger / get ignored)
  2. Status classification (DELIVERED / MISSED / PENDING under various
     `evaluation_time` values)
  3. Aggregate ratio computation and the [0,5] mapping
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.commitments import (
    GRACE_MINUTES,
    VAGUE_DEFAULT_MINUTES_CHAT,
    VAGUE_DEFAULT_MINUTES_EMAIL,
    analyze_commitments,
)
from app.models import Message


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 6, 15, hh, mm, tzinfo=timezone.utc)


def _msg(ts: datetime, author: str, body: str) -> Message:
    return Message(timestamp=ts, author_type=author, body=body)  # type: ignore[arg-type]


# ---------- detection: numeric singles ----------


def test_single_minutes_detected():
    msgs = [_msg(_at(10, 0), "agent", "I'll respond in 10 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 1
    assert a.missed == 1
    assert a.commitments[0].deadline == _at(10, 10)


def test_single_min_short_form():
    msgs = [_msg(_at(10, 0), "agent", "give me 5 min to check")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 1


def test_single_hours_converted_correctly():
    msgs = [_msg(_at(10, 0), "agent", "I'll get back to you in 2 hours")]
    a = analyze_commitments(msgs, evaluation_time=_at(13, 0))
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(12, 0)


def test_zero_minutes_ignored():
    msgs = [_msg(_at(10, 0), "agent", "in 0 minutes I will reply")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


def test_absurdly_large_minutes_ignored():
    """Don't accept '5000 minutes' as a real commitment — likely noise."""
    msgs = [_msg(_at(10, 0), "agent", "in 5000 minutes I will reply")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


# ---------- detection: ranges (use upper bound) ----------


def test_range_uses_upper_bound():
    msgs = [_msg(_at(10, 0), "agent", "give me 10-15 minutes to investigate")]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(10, 15)


def test_range_with_to_word():
    msgs = [_msg(_at(10, 0), "agent", "this will take 5 to 10 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(10, 10)


def test_range_en_dash():
    msgs = [_msg(_at(10, 0), "agent", "I'll respond in 10–15 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(10, 15)


def test_range_not_double_counted_as_single():
    """'10-15 min' should be one commitment (upper-bound), not two singles."""
    msgs = [_msg(_at(10, 0), "agent", "give me 10-15 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.detected == 1


# ---------- detection: vague phrases ----------


def test_vague_phrase_chat_channel_uses_1h():
    msgs = [_msg(_at(10, 0), "agent", "let me check and revert")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 5), channel="native_messaging")
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(10, 0) + timedelta(minutes=VAGUE_DEFAULT_MINUTES_CHAT)
    assert a.commitments[0].deadline == _at(11, 0)  # +60 min


def test_vague_phrase_email_channel_uses_24h():
    msgs = [_msg(_at(10, 0), "agent", "let me check and revert")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 5), channel="email")
    assert a.detected == 1
    assert a.commitments[0].deadline == _at(10, 0) + timedelta(minutes=VAGUE_DEFAULT_MINUTES_EMAIL)


def test_vague_phrase_unknown_channel_defaults_to_email():
    """Conservative default — when channel is unknown, use the 24h window."""
    msgs = [_msg(_at(10, 0), "agent", "let me check and revert")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 5), channel="")
    assert a.commitments[0].deadline == _at(10, 0) + timedelta(minutes=VAGUE_DEFAULT_MINUTES_EMAIL)


def test_vague_phrase_web_service_uses_24h():
    """web_service is Zendesk's tag for email-style tickets — also 24h."""
    msgs = [_msg(_at(10, 0), "agent", "let me check and revert")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 5), channel="web_service")
    assert a.commitments[0].deadline == _at(10, 0) + timedelta(minutes=VAGUE_DEFAULT_MINUTES_EMAIL)


def test_vague_skipped_when_numeric_present():
    """If we found a number, don't ALSO add a vague match in the same msg."""
    msgs = [_msg(_at(10, 0), "agent", "let me check, give me 5 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 1  # not 2


# ---------- detection: false-positive guards ----------


def test_past_tense_minutes_ago_not_a_commitment():
    msgs = [
        _msg(_at(10, 5), "agent", "I sent that 10 minutes ago, did you receive it?"),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


def test_past_tense_earlier_not_a_commitment():
    msgs = [_msg(_at(10, 5), "agent", "the system rebooted 30 minutes earlier")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


def test_customer_promise_does_not_count():
    """Only agent commitments are tracked."""
    msgs = [_msg(_at(10, 0), "customer", "I'll send the logs in 5 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


def test_empty_body_ignored():
    msgs = [_msg(_at(10, 0), "agent", "")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 30))
    assert a.detected == 0


# ---------- status: DELIVERED / MISSED / PENDING ----------


def test_pending_when_deadline_not_yet_passed():
    """The exact bug from ticket 71726: eval runs before deadline."""
    msgs = [_msg(_at(10, 0), "agent", "give me 10-15 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 5))
    assert a.detected == 1
    assert a.pending == 1
    assert a.missed == 0
    assert a.commitments[0].status == "PENDING"
    assert a.ratio_value == 0.0


def test_flips_to_missed_immediately_at_deadline():
    """No grace by default — past the deadline by any amount is MISSED."""
    msgs = [_msg(_at(10, 0), "agent", "give me 10 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 11))  # 1 min past
    assert a.commitments[0].status == "MISSED"
    assert a.missed == 1


def test_grace_minutes_is_zero_by_default():
    """GRACE_MINUTES constant is 0 — codifies the no-grace policy."""
    assert GRACE_MINUTES == 0


def test_grace_minutes_param_still_works_when_passed():
    """We keep the param for tests / future use, but default is 0."""
    msgs = [_msg(_at(10, 0), "agent", "give me 10 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(10, 11), grace_minutes=5)
    assert a.commitments[0].status == "PENDING"  # 10:11 < 10:10 + 5min grace


def test_delivered_when_agent_replies_in_time():
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes to check"),
        _msg(_at(10, 5), "customer", "ok"),
        _msg(_at(10, 8), "agent", "here's what I found ..."),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.delivered == 1
    assert a.missed == 0
    assert a.pending == 0
    assert a.commitments[0].fulfilled_at == _at(10, 8)


def test_missed_when_agent_replied_even_one_minute_late():
    """No grace — 1 minute past deadline is MISSED."""
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes"),
        _msg(_at(10, 11), "agent", "done"),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.missed == 1
    assert a.delivered == 0


def test_delivered_when_agent_replied_at_exact_deadline():
    """A reply AT the deadline counts (deadline is inclusive)."""
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes"),
        _msg(_at(10, 10), "agent", "done"),  # exactly on deadline
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.delivered == 1
    assert a.missed == 0


def test_missed_when_agent_replied_too_late():
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes"),
        _msg(_at(10, 30), "agent", "sorry for the delay"),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.missed == 1
    assert a.delivered == 0


def test_multiple_commitments_one_delivered_one_missed():
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes for the logs"),
        _msg(_at(10, 5), "agent", "logs are: ..."),                # delivered the first
        _msg(_at(11, 0), "agent", "give me 5 minutes for the fix"), # second commitment
        _msg(_at(11, 30), "customer", "still waiting"),
    ]
    # eval at 12:00 — second deadline (11:05+grace 11:07) long since elapsed
    a = analyze_commitments(msgs, evaluation_time=_at(12, 0))
    assert a.delivered == 1
    assert a.missed == 1
    assert a.pending == 0
    assert a.ratio_value == pytest.approx((1 / 2) * 4.0)


def test_two_commitments_in_one_message():
    msgs = [
        _msg(_at(10, 0), "agent", "I'll check in 5 minutes and have an answer within 30 minutes"),
        _msg(_at(10, 4), "agent", "still looking"),  # fulfills both before either deadline
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    # Both deadlines (10:05 and 10:30) are >= 10:04, so the single agent reply
    # at 10:04 fulfills both.
    assert a.detected == 2
    assert a.delivered == 2


# ---------- ratio computation ----------


def test_ratio_zero_when_no_commitments():
    a = analyze_commitments([], evaluation_time=_at(10, 0))
    assert a.ratio_value == 0.0


def test_ratio_zero_when_all_pending():
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes"),
        _msg(_at(11, 0), "agent", "give me 5 minutes"),
    ]
    # eval just after both made, neither deadline elapsed yet
    a = analyze_commitments(msgs, evaluation_time=_at(11, 2))
    # NB: first commitment's deadline = 10:10, but second agent message at 11:00 is well past
    #     that, so first becomes MISSED. Second commitment is PENDING.
    # To test ratio_value==0 when ALL pending, use two simultaneous pending commits.
    msgs2 = [_msg(_at(11, 0), "agent", "give me 10 minutes and also 5 min")]
    a2 = analyze_commitments(msgs2, evaluation_time=_at(11, 2))
    assert a2.pending == 2
    assert a2.delivered == 0
    assert a2.missed == 0
    assert a2.ratio_value == 0.0


def test_ratio_maxes_when_all_missed():
    msgs = [_msg(_at(10, 0), "agent", "give me 5 minutes")]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    assert a.missed == 1
    assert a.delivered == 0
    assert a.ratio_value == 4.0


def test_summary_lines_human_readable():
    msgs = [
        _msg(_at(10, 0), "agent", "give me 10 minutes"),
        _msg(_at(10, 5), "agent", "done"),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(11, 0))
    lines = a.as_summary_lines()
    # spot-check structure
    joined = "\n".join(lines)
    assert "delivered: 1" in joined
    assert "DELIVERED" in joined
    assert "fulfilled at" in joined


# ---------- regression: the exact 71726 scenario ----------


def test_ticket_71726_scenario_does_not_misfire():
    """At 05:53 agent says '10-15 minutes'. At 05:56 (3 min later) the
    engine evaluates. CTDR must be 0, not 5 — the deadline is still 6-12
    minutes in the future.
    """
    msgs = [
        _msg(_at(5, 53), "agent", "give me 10-15 minutes to investigate"),
        _msg(_at(5, 54), "customer", "ok"),
    ]
    a = analyze_commitments(msgs, evaluation_time=_at(5, 56))
    assert a.commitments[0].status == "PENDING"
    assert a.ratio_value == 0.0
