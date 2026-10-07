"""Round-trip tests for the shared cross-step (de)serialization (FEAT-001).

The ``shared.events`` module converts the real domain types to/from JSON-safe dicts so
they can travel between Step Functions states. Every converter must round-trip losslessly
(reuse the domain types; redefine nothing), and the ``EnrichmentResult`` variants must
serialize into the documented ``{outcome, attributes, reason, attempts}`` envelope.
"""

from __future__ import annotations

import json

from handlers.shared import events

from movieintel.data_access.models import MovieRow, RatingScale, SampledMovie
from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
)
from movieintel.enrichment.results import Blocked, Failed, Ok, Repaired
from movieintel.eval.models import EvalRecord
from movieintel.persistence.item import EnrichedMovie


def _sampled() -> SampledMovie:
    return SampledMovie(
        movie=MovieRow(
            movie_id=7,
            title="A Film",
            overview="An overview.",
            genres='["Drama"]',
            language="en",
            release_date="2020-01-01",
            budget=1_000_000,
            revenue=5_000_000,
            runtime=120.0,
        ),
        rating=4.0,
        rating_count=10,
    )


def _scale() -> RatingScale:
    return RatingScale(
        observed_min=0.5,
        observed_max=5.0,
        declared_max=5.0,
        effective_max=5.0,
        fallback_used=False,
        fallback_default=5.0,
    )


def _effectiveness() -> EffectivenessScore:
    return EffectivenessScore(
        value=84.85,
        roi_defined=True,
        rating_defined=True,
        tier=PESTier.STANDOUT,
        explanation="Strong ROI.",
    )


def _attributes() -> EnrichmentAttributes:
    return EnrichmentAttributes(
        overview_sentiment=Sentiment.POSITIVE,
        budget_tier="medium",
        revenue_tier="high",
        effectiveness=_effectiveness(),
        mood=Mood.UPLIFTING,
        reasoning_basis="Revenue above budget; positive overview.",
    )


def _enriched() -> EnrichedMovie:
    return EnrichedMovie(movie=_sampled(), attributes=_attributes())


def _json_safe(data: object) -> None:
    """Assert the structure survives a JSON encode/decode cycle (Step Functions payload)."""
    assert json.loads(json.dumps(data)) == data


# ------------------------------------------------------------------- SampledMovie


def test_sampled_movie_round_trips() -> None:
    original = _sampled()
    payload = events.sampled_to_json(original)
    _json_safe(payload)
    assert events.sampled_from_json(payload) == original


# -------------------------------------------------------------------- RatingScale


def test_rating_scale_round_trips() -> None:
    original = _scale()
    payload = events.scale_to_json(original)
    _json_safe(payload)
    assert events.scale_from_json(payload) == original


# ------------------------------------------------------------- EffectivenessScore


def test_effectiveness_round_trips() -> None:
    original = _effectiveness()
    payload = events.effectiveness_to_json(original)
    _json_safe(payload)
    assert events.effectiveness_from_json(payload) == original


# --------------------------------------------------------- EnrichmentAttributes


def test_attributes_round_trips() -> None:
    original = _attributes()
    payload = events.attributes_to_json(original)
    _json_safe(payload)
    assert events.attributes_from_json(payload) == original


# ------------------------------------------------------------------ EnrichedMovie


def test_enriched_round_trips() -> None:
    original = _enriched()
    payload = events.enriched_to_json(original)
    _json_safe(payload)
    assert events.enriched_from_json(payload) == original


# -------------------------------------------------------------------- EvalRecord


def test_eval_record_round_trips() -> None:
    original = EvalRecord(
        movie_id=7,
        produced=_attributes(),
        raw_payload=None,
        computed_pes=84.85,
        expected_pes=84.85,
        expected_sentiment=Sentiment.POSITIVE,
        expected_mood=Mood.UPLIFTING,
        guardrail_attached=True,
    )
    payload = events.eval_record_to_json(original)
    _json_safe(payload)
    assert events.eval_record_from_json(payload) == original


def test_eval_record_round_trips_schema_failure() -> None:
    """A schema-invalid record (produced is None, raw_payload present) round-trips too."""
    original = EvalRecord(
        movie_id=9,
        produced=None,
        raw_payload={"overview_sentiment": "ecstatic"},
        computed_pes=10.0,
        expected_pes=10.0,
        expected_sentiment=Sentiment.NEUTRAL,
        expected_mood=Mood.DARK,
        guardrail_attached=True,
    )
    payload = events.eval_record_to_json(original)
    _json_safe(payload)
    assert events.eval_record_from_json(payload) == original


# ----------------------------------------------------- EnrichmentResult envelope


def test_result_to_json_ok() -> None:
    payload = events.result_to_json(Ok(attributes=_attributes()))
    _json_safe(payload)
    assert payload["outcome"] == "ok"
    assert payload["attributes"] == events.attributes_to_json(_attributes())
    assert payload["reason"] is None
    assert payload["attempts"] == 0


def test_result_to_json_repaired() -> None:
    payload = events.result_to_json(Repaired(attributes=_attributes(), attempts=2))
    _json_safe(payload)
    assert payload["outcome"] == "repaired"
    assert payload["attributes"] == events.attributes_to_json(_attributes())
    assert payload["reason"] is None
    assert payload["attempts"] == 2


def test_result_to_json_failed() -> None:
    payload = events.result_to_json(Failed(reason="repair exhausted"))
    _json_safe(payload)
    assert payload["outcome"] == "failed"
    assert payload["attributes"] is None
    assert payload["reason"] == "repair exhausted"
    assert payload["attempts"] == 0


def test_result_to_json_blocked() -> None:
    payload = events.result_to_json(Blocked(reason="guardrail intervened"))
    _json_safe(payload)
    assert payload["outcome"] == "blocked"
    assert payload["attributes"] is None
    assert payload["reason"] == "guardrail intervened"
    assert payload["attempts"] == 0
