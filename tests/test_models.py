from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import SentimentResponse


def _valid_response_kwargs(**overrides):
    base = {
        "frustration": 2.0,
        "urgency": 2.0,
        "churn_threat": 2.0,
        "confusion": 2.0,
        "politeness_erosion": 2.0,
        "dissatisfaction_trajectory": 2.0,
        "agent_tone": 2.0,
        "commitment_to_delivery_ratio": 2.0,
        "commitments_detected": 1,
        "commitments_missed": 0,
        "top_signal": "frustration",
        "confidence": 3.0,
        "reasoning": "test reasoning",
    }
    base.update(overrides)
    return base


def test_valid_response_parses():
    r = SentimentResponse(**_valid_response_kwargs())
    assert r.frustration == 2.0


def test_out_of_range_sub_score_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(frustration=5.1))


def test_negative_sub_score_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(urgency=-0.1))


def test_unknown_top_signal_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(top_signal="not_a_real_signal"))


def test_missed_exceeds_detected_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(
            **_valid_response_kwargs(commitments_detected=1, commitments_missed=2)
        )


def test_empty_reasoning_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(reasoning=""))


def test_reasoning_at_v5_cap_accepted():
    """Prompt v5 asks for evidence-citing reasoning, which runs past the old
    500-char cap. A real ticket produced 566 chars and lost its whole
    evaluation, so the cap is now 1200."""
    SentimentResponse(**_valid_response_kwargs(reasoning="x" * 1200))


def test_reasoning_over_cap_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(reasoning="x" * 1201))


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SentimentResponse(**_valid_response_kwargs(confidence=5.5))
