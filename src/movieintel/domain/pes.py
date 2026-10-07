"""Deterministic Production Effectiveness Score (PES) — pure, no LLM/I/O (REQ-A-3).

PES blends a logistic-normalized ROI with a normalized rating, scaled to 0–100
(design §3.2 / §5.3). The function is pure and reproducible: identical inputs
yield an identical ``value`` (REQ-A-3.2). The rating-normalization scale is
*passed in* (``RatingScale.effective_max``, derived from the introspected ratings
data per REQ-A-1.6); this module defines NO ``RATING_MAX`` constant, so PES never
silently assumes a ratings maximum (design §7.2). Any fallback default lives on
``RatingScale`` itself (``fallback_used``/``fallback_default``), surfaced loudly.

Safe-handling (design §3.2):
- ``budget <= 0`` or ``budget is None`` -> ROI undefined; rating-only value and
  ``roi_defined=False`` (no divide-by-zero, REQ-A-3.3).
- ``rating is None`` -> documented honest-missing rule: ``rating_norm = 0.0`` and
  ``rating_defined=False`` (contributes nothing rather than imputing a value,
  REQ-A-2.4). The combined edge (no budget, no rating) yields a defensible 0.0.
- ``revenue is None`` with ``budget > 0`` -> revenue treated as 0, so ROI is a
  defined negative value and ``roi_defined=True`` (budget>0 means ROI is
  computable; REQ-A-3.3 only guards divide-by-zero).

``tier``/``explanation`` on the returned ``EffectivenessScore`` are LLM-assigned
in Task 3 and left at their defaults here.
"""

from __future__ import annotations

import math

from movieintel.data_access.models import RatingScale, SampledMovie
from movieintel.domain.schemas import EffectivenessScore

# Documented default weights (design §3.2). Overridable via function parameters.
W_ROI = 0.6
W_RATING = 0.4


def _logistic(x: float) -> float:
    """Standard logistic ``1 / (1 + exp(-x))`` squashing ROI into (0, 1)."""
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp ``x`` into ``[lo, hi]`` (defaults to the unit interval)."""
    return max(lo, min(hi, x))


def production_effectiveness_score(
    *,
    budget: int | None,
    revenue: int | None,
    rating: float | None,
    effective_max: float,
    w_roi: float = W_ROI,
    w_rating: float = W_RATING,
) -> EffectivenessScore:
    """Compute the deterministic PES for scalar inputs (design §3.2).

    ``effective_max`` is the data-derived rating scale (``RatingScale.effective_max``)
    and must be passed by the caller; it is never assumed here.
    """
    if rating is None:
        rating_norm = 0.0
        rating_defined = False
    else:
        rating_norm = _clamp(rating / effective_max)
        rating_defined = True

    if budget is None or budget <= 0:
        # Rating-only safe-handling branch — no divide-by-zero (REQ-A-3.3).
        value = round(100.0 * rating_norm, 2)
        return EffectivenessScore(value=value, roi_defined=False, rating_defined=rating_defined)

    # Missing revenue with a valid budget: treat as 0 so ROI is a defined negative.
    rev = 0 if revenue is None else revenue
    roi = (rev - budget) / budget
    roi_norm = _clamp(_logistic(roi))
    value = round(100.0 * (w_roi * roi_norm + w_rating * rating_norm), 2)
    return EffectivenessScore(value=value, roi_defined=True, rating_defined=rating_defined)


def score_sampled_movie(
    sampled: SampledMovie,
    scale: RatingScale,
    *,
    w_roi: float = W_ROI,
    w_rating: float = W_RATING,
) -> EffectivenessScore:
    """Adapter: compute PES from Task 1 types (``SampledMovie`` + ``RatingScale``).

    Unpacks the scalar inputs and delegates to
    :func:`production_effectiveness_score`; this is the real integration point
    with the data-access layer.
    """
    return production_effectiveness_score(
        budget=sampled.movie.budget,
        revenue=sampled.movie.revenue,
        rating=sampled.rating,
        effective_max=scale.effective_max,
        w_roi=w_roi,
        w_rating=w_rating,
    )
