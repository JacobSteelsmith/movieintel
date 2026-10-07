"""Cross-step (de)serialization between Step Functions states (Task 6, FEAT-001).

Each Step Functions state passes JSON to the next, so the domain types that travel
between the enrichment Lambdas must round-trip through JSON-safe dicts. These converters
reuse the real ``movieintel`` domain types and redefine nothing:

- The Pydantic types (``SampledMovie``, ``RatingScale``, ``EffectivenessScore``,
  ``EnrichmentAttributes``, ``EvalRecord``) serialize via ``model_dump(mode="json")`` and
  rebuild via ``model_validate`` so enums and floats come back as the exact same objects.
- ``EnrichedMovie`` is a frozen dataclass wrapping two Pydantic models, so it maps to
  ``{"movie": ..., "attributes": ...}`` explicitly.
- The ``EnrichmentResult`` union (``Ok``/``Repaired``/``Failed``/``Blocked`` frozen
  dataclasses) serializes into one documented envelope
  ``{"outcome", "attributes"|null, "reason"|null, "attempts"}`` so a downstream state can
  branch on ``outcome`` without reconstructing the Python type.
"""

from __future__ import annotations

from typing import Any

from movieintel.data_access.models import RatingScale, SampledMovie
from movieintel.domain.schemas import EffectivenessScore, EnrichmentAttributes
from movieintel.enrichment.results import EnrichmentResult, Failed, Ok, Repaired
from movieintel.eval.models import EvalRecord
from movieintel.persistence.item import EnrichedMovie

# JSON-safe payload type carried between Step Functions states.
JsonDict = dict[str, Any]


def sampled_to_json(movie: SampledMovie) -> JsonDict:
    """Serialize a :class:`SampledMovie` to a JSON-safe dict."""
    return movie.model_dump(mode="json")


def sampled_from_json(payload: JsonDict) -> SampledMovie:
    """Reconstruct a :class:`SampledMovie` from its JSON-safe dict."""
    return SampledMovie.model_validate(payload)


def scale_to_json(scale: RatingScale) -> JsonDict:
    """Serialize a :class:`RatingScale` to a JSON-safe dict."""
    return scale.model_dump(mode="json")


def scale_from_json(payload: JsonDict) -> RatingScale:
    """Reconstruct a :class:`RatingScale` from its JSON-safe dict."""
    return RatingScale.model_validate(payload)


def effectiveness_to_json(score: EffectivenessScore) -> JsonDict:
    """Serialize an :class:`EffectivenessScore` to a JSON-safe dict."""
    return score.model_dump(mode="json")


def effectiveness_from_json(payload: JsonDict) -> EffectivenessScore:
    """Reconstruct an :class:`EffectivenessScore` from its JSON-safe dict."""
    return EffectivenessScore.model_validate(payload)


def attributes_to_json(attributes: EnrichmentAttributes) -> JsonDict:
    """Serialize :class:`EnrichmentAttributes` to a JSON-safe dict."""
    return attributes.model_dump(mode="json")


def attributes_from_json(payload: JsonDict) -> EnrichmentAttributes:
    """Reconstruct :class:`EnrichmentAttributes` from its JSON-safe dict."""
    return EnrichmentAttributes.model_validate(payload)


def enriched_to_json(record: EnrichedMovie) -> JsonDict:
    """Serialize an :class:`EnrichedMovie` to ``{"movie": .., "attributes": ..}``."""
    return {
        "movie": sampled_to_json(record.movie),
        "attributes": attributes_to_json(record.attributes),
    }


def enriched_from_json(payload: JsonDict) -> EnrichedMovie:
    """Reconstruct an :class:`EnrichedMovie` from its JSON-safe dict."""
    return EnrichedMovie(
        movie=sampled_from_json(payload["movie"]),
        attributes=attributes_from_json(payload["attributes"]),
    )


def eval_record_to_json(record: EvalRecord) -> JsonDict:
    """Serialize an :class:`EvalRecord` to a JSON-safe dict."""
    return record.model_dump(mode="json")


def eval_record_from_json(payload: JsonDict) -> EvalRecord:
    """Reconstruct an :class:`EvalRecord` from its JSON-safe dict."""
    return EvalRecord.model_validate(payload)


def result_to_json(result: EnrichmentResult) -> JsonDict:
    """Serialize an enrichment outcome into the shared envelope.

    The envelope is ``{"outcome", "attributes"|null, "reason"|null, "attempts"}``:
    ``Ok``/``Repaired`` carry validated attributes (``reason`` null); ``Failed``/
    ``Blocked`` carry a reason (``attributes`` null). ``attempts`` is the repair count
    (``Repaired`` only; 0 otherwise).
    """
    if isinstance(result, Ok):
        return {
            "outcome": "ok",
            "attributes": attributes_to_json(result.attributes),
            "reason": None,
            "attempts": 0,
        }
    if isinstance(result, Repaired):
        return {
            "outcome": "repaired",
            "attributes": attributes_to_json(result.attributes),
            "reason": None,
            "attempts": result.attempts,
        }
    if isinstance(result, Failed):
        return {
            "outcome": "failed",
            "attributes": None,
            "reason": result.reason,
            "attempts": 0,
        }
    return {
        "outcome": "blocked",
        "attributes": None,
        "reason": result.reason,
        "attempts": 0,
    }
