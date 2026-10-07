"""Domain layer: deterministic PES + enrichment Pydantic schemas (REQ-A-2, A-3, X-1).

Pure logic only — no LLM, no I/O, no randomness. Public API re-exported for
ergonomic imports, mirroring the data-access layer.
"""

from __future__ import annotations

from movieintel.domain.pes import (
    W_RATING,
    W_ROI,
    production_effectiveness_score,
    score_sampled_movie,
)
from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
    Tier,
    TierOrUnknown,
)

__all__ = [
    "W_RATING",
    "W_ROI",
    "EffectivenessScore",
    "EnrichmentAttributes",
    "Mood",
    "PESTier",
    "Sentiment",
    "Tier",
    "TierOrUnknown",
    "production_effectiveness_score",
    "score_sampled_movie",
]
