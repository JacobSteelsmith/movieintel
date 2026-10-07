"""Validate state handler: re-validate attributes, merge PES, emit EnrichedMovie.

Thin wrapper over the domain schemas; it duplicates no validation logic. On an
``ok``/``repaired`` upstream outcome it re-validates the attributes dict against
:class:`EnrichmentAttributes` and pins the deterministic PES fields (``value``,
``roi_defined``, ``rating_defined``) from the computed score so the LLM cannot alter them
(REQ-A-3.4), then assembles an :class:`EnrichedMovie`. On a ``failed``/``blocked`` outcome
it short-circuits to a recorded skip so one movie's failure never aborts the batch
(REQ-A-4.3, REQ-A-6.2).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.domain.schemas import EffectivenessScore, EnrichmentAttributes
from movieintel.persistence.item import EnrichedMovie

# Upstream outcomes that carry no attributes and must be recorded as a skip.
_SKIP_OUTCOMES = ("failed", "blocked")


def _merge_pes(attributes: EnrichmentAttributes, score: EffectivenessScore) -> EnrichmentAttributes:
    """Return attributes with the deterministic PES fields pinned from ``score``.

    The LLM-assigned ``tier``/``explanation`` are preserved; the deterministic
    ``value``/``roi_defined``/``rating_defined`` are taken from the computed score.
    """
    merged_effectiveness = attributes.effectiveness.model_copy(
        update={
            "value": score.value,
            "roi_defined": score.roi_defined,
            "rating_defined": score.rating_defined,
        }
    )
    return attributes.model_copy(update={"effectiveness": merged_effectiveness})


def handler(event: dict[str, Any], context: Any) -> JsonDict:
    """Validate the enriched attributes and emit an EnrichedMovie, or record a skip."""
    sampled = events.sampled_from_json(event["movie"])
    score = events.effectiveness_from_json(event["score"])
    result = event["result"]
    outcome = result["outcome"]

    if outcome in _SKIP_OUTCOMES:
        return {
            "status": "skipped",
            "movie_id": sampled.movie.movie_id,
            "outcome": outcome,
            "reason": result.get("reason"),
        }

    attributes = EnrichmentAttributes.model_validate(result["attributes"])
    attributes = _merge_pes(attributes, score)
    enriched = EnrichedMovie(movie=sampled, attributes=attributes)

    return {
        "status": "enriched",
        "movie_id": sampled.movie.movie_id,
        "outcome": outcome,
        "enriched": events.enriched_to_json(enriched),
    }
