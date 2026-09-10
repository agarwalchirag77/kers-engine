from __future__ import annotations

from .models import SUB_SCORE_NAMES, SentimentBreakdown


def compute_ers(breakdown: SentimentBreakdown, weights: dict[str, float]) -> float:
    """Pure weighted sum. Sub-scores are in [0, 4]; weights sum to 1.00;
    result is in [0, 4]."""
    return sum(weights[name] * getattr(breakdown, name) for name in SUB_SCORE_NAMES)
