"""Independent reference PES implementation for non-circular exactness tests.

This file is written by hand from the design §3.2 formula and is deliberately
NOT importing ``movieintel.domain.pes``. It exists only so the exactness tests
can cross-check the production function against an independent calculation
(REQ-X-2.5). It is a test helper and must never be imported by production code.
"""

from __future__ import annotations

import math

_W_ROI = 0.6
_W_RATING = 0.4


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def reference_pes_value(
    *,
    budget: int | None,
    revenue: int | None,
    rating: float | None,
    effective_max: float,
    w_roi: float = _W_ROI,
    w_rating: float = _W_RATING,
) -> float:
    """Recompute PES ``value`` independently of the production implementation."""
    rating_norm = 0.0 if rating is None else _clamp(rating / effective_max)

    if budget is None or budget <= 0:
        return round(100.0 * rating_norm, 2)

    rev = 0 if revenue is None else revenue
    roi = (rev - budget) / budget
    roi_norm = _clamp(_logistic(roi))
    return round(100.0 * (w_roi * roi_norm + w_rating * rating_norm), 2)
