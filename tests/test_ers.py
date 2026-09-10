from __future__ import annotations

import pytest

from app.ers import compute_ers
from app.models import SUB_SCORE_NAMES, SentimentBreakdown


def make_breakdown(value: float) -> SentimentBreakdown:
    return SentimentBreakdown(**{name: value for name in SUB_SCORE_NAMES})


def test_zeros_give_zero(default_weight_config):
    assert compute_ers(make_breakdown(0.0), default_weight_config.weights) == 0.0


def test_max_gives_four(default_weight_config):
    # Each sub-score at 4.0 (the v5 ceiling), weights sum to 1 → ERS = 4.0
    ers = compute_ers(make_breakdown(4.0), default_weight_config.weights)
    assert ers == pytest.approx(4.0)


def test_uniform_gives_value(default_weight_config):
    ers = compute_ers(make_breakdown(2.5), default_weight_config.weights)
    assert ers == pytest.approx(2.5)


def test_only_churn_threat(default_weight_config):
    """If only churn_threat is maxed (4.0) and others zero, ERS = weight * 4."""
    breakdown = SentimentBreakdown(
        frustration=0.0,
        urgency=0.0,
        churn_threat=4.0,
        confusion=0.0,
        politeness_erosion=0.0,
        dissatisfaction_trajectory=0.0,
        agent_tone=0.0,
        commitment_to_delivery_ratio=0.0,
    )
    ers = compute_ers(breakdown, default_weight_config.weights)
    assert ers == pytest.approx(0.25 * 4.0)


def test_weights_sum_to_one(default_weight_config):
    """v1 invariant: weights sum to 1.00."""
    assert sum(default_weight_config.weights.values()) == pytest.approx(1.0)


def test_all_sub_scores_have_weights(default_weight_config):
    """Every sub-score must have a weight."""
    assert set(default_weight_config.weights.keys()) == set(SUB_SCORE_NAMES)
