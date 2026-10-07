"""Fixtures for handler tests: moto DDB table, scripted Converse client, fakes, builders.

These mirror the established project patterns so the handlers are exercised against
faithful mocks (REQ-X-3.4): the ``dynamodb_table`` fixture copies the key schema from
``tests/persistence/conftest.py``; the scripted Converse client mirrors
``tests/enrichment/test_enrich_movie.py``; ``FakeEmbedder``/``FakeVectorWriter`` reuse the
persistence fakes. Event-dict builders assemble the JSON-safe payloads that travel between
Step Functions states so each handler test feeds a realistic event.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from handlers.shared import events
from moto import mock_aws

from movieintel.data_access.models import MovieRow, RatingScale, SampledMovie
from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
)
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie


@pytest.fixture
def persistence_config() -> PersistenceConfig:
    """Default persistence config (table ``MovieIntel``, GSI ``GSI1``)."""
    return PersistenceConfig.from_env(env={})


@pytest.fixture
def dynamodb_table(persistence_config: PersistenceConfig) -> Iterator[Any]:
    """A moto-backed ``MovieIntel`` table with the design key schema + GSI1.

    Copies the schema from ``tests/persistence/conftest.py``: PK/SK HASH/RANGE, GSI1 on
    GSI1PK/GSI1SK ProjectionType ALL, BillingMode PAY_PER_REQUEST.
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=persistence_config.region)
        table = resource.create_table(
            TableName=persistence_config.table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": persistence_config.gsi1_name,
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


# ------------------------------------------------- scripted Bedrock Converse client


def converse_response(
    text: str,
    *,
    stop_reason: str = "end_turn",
    guardrail: bool = False,
) -> dict[str, Any]:
    """Build a realistic Converse response envelope (mirrors the enrichment tests)."""
    usage = {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30}
    metrics = {"latencyMs": 100}
    if guardrail:
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": ""}]}},
            "stopReason": "guardrail_intervened",
            "usage": usage,
            "metrics": metrics,
            "trace": {"guardrail": {"outputAssessments": {}}},
        }
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": usage,
        "metrics": metrics,
    }


class ScriptedConverseClient:
    """A scripted Converse client that records every request it receives."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("converse called more times than scripted")
        return self._responses.pop(0)


def valid_attributes_payload() -> dict[str, Any]:
    """A schema-valid EnrichmentAttributes payload the model would emit."""
    return {
        "overview_sentiment": "positive",
        "budget_tier": "medium",
        "revenue_tier": "high",
        "effectiveness": {
            "value": 84.85,
            "tier": "standout",
            "explanation": "Strong ROI and solid audience rating.",
            "roi_defined": True,
            "rating_defined": True,
        },
        "mood": "uplifting",
        "reasoning_basis": "Revenue well above budget; positive overview.",
    }


# ---------------------------------------------------------- Fake embedder/writer


class FakeEmbedder:
    """A stub embedder returning a fixed vector and recording the text it saw."""

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.texts: list[str] = []

    def embed_overview(self, text: str) -> list[float]:
        self.texts.append(text)
        return self.vector


class FakeVectorWriter:
    """A stub vector writer recording every upsert call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[float], dict[str, Any]]] = []

    def upsert_vector(self, movie_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        self.calls.append((movie_id, vector, metadata))


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fake_writer() -> FakeVectorWriter:
    return FakeVectorWriter()


# ---------------------------------------------------------------- domain builders


def make_sampled(
    *,
    movie_id: int = 42,
    title: str = "The Example",
    overview: str | None = "A test film about testing.",
    budget: int | None = 1_000_000,
    revenue: int | None = 5_000_000,
    runtime: float | None = 120.0,
    rating: float | None = 4.0,
    rating_count: int = 10,
    genres: str | None = '["Drama"]',
) -> SampledMovie:
    """Build a :class:`SampledMovie` from the real domain types (builder for tests)."""
    return SampledMovie(
        movie=MovieRow(
            movie_id=movie_id,
            title=title,
            overview=overview,
            genres=genres,
            language="en",
            release_date="2020-01-01",
            budget=budget,
            revenue=revenue,
            runtime=runtime,
        ),
        rating=rating,
        rating_count=rating_count,
    )


def make_scale(*, effective_max: float = 5.0) -> RatingScale:
    """Build a :class:`RatingScale` with an observed maximum (no fallback)."""
    return RatingScale(
        observed_min=0.5,
        observed_max=effective_max,
        declared_max=5.0,
        effective_max=effective_max,
        fallback_used=False,
        fallback_default=5.0,
    )


def make_effectiveness(
    *,
    value: float = 84.85,
    roi_defined: bool = True,
    rating_defined: bool = True,
    tier: PESTier | None = None,
    explanation: str = "",
) -> EffectivenessScore:
    """Build an :class:`EffectivenessScore` (deterministic PES result)."""
    return EffectivenessScore(
        value=value,
        roi_defined=roi_defined,
        rating_defined=rating_defined,
        tier=tier,
        explanation=explanation,
    )


def make_attributes(
    *,
    sentiment: Sentiment = Sentiment.POSITIVE,
    mood: Mood = Mood.UPLIFTING,
    effectiveness: EffectivenessScore | None = None,
) -> EnrichmentAttributes:
    """Build :class:`EnrichmentAttributes` with a standout PES by default."""
    return EnrichmentAttributes(
        overview_sentiment=sentiment,
        budget_tier="medium",
        revenue_tier="high",
        effectiveness=effectiveness
        or make_effectiveness(tier=PESTier.STANDOUT, explanation="Strong ROI."),
        mood=mood,
        reasoning_basis="Revenue above budget; positive overview.",
    )


def make_enriched(**kwargs: Any) -> EnrichedMovie:
    """Build an :class:`EnrichedMovie` (sampled movie + attributes)."""
    sampled = kwargs.pop("sampled", None) or make_sampled()
    attributes = kwargs.pop("attributes", None) or make_attributes()
    return EnrichedMovie(movie=sampled, attributes=attributes)


# -------------------------------------------------------------- event-dict builders


def compute_pes_event(
    *, sampled: SampledMovie | None = None, scale: RatingScale | None = None
) -> dict[str, Any]:
    """Build a ComputePES event ``{"movie": sampled_json, "scale": scale_json}``."""
    return {
        "movie": events.sampled_to_json(sampled or make_sampled()),
        "scale": events.scale_to_json(scale or make_scale()),
    }


def enrich_event(
    *, sampled: SampledMovie | None = None, score: EffectivenessScore | None = None
) -> dict[str, Any]:
    """Build an Enrich event ``{"movie": sampled_json, "score": effectiveness_json}``."""
    return {
        "movie": events.sampled_to_json(sampled or make_sampled()),
        "score": events.effectiveness_to_json(score or make_effectiveness()),
    }


def eval_record_json(
    *,
    movie_id: int = 42,
    produced: EnrichmentAttributes | None = None,
    raw_payload: dict[str, Any] | None = None,
    computed_pes: float = 84.85,
    expected_pes: float = 84.85,
    expected_sentiment: Sentiment = Sentiment.POSITIVE,
    expected_mood: Mood = Mood.UPLIFTING,
    guardrail_attached: bool = True,
) -> dict[str, Any]:
    """Build a JSON-safe EvalRecord payload for the Evaluate handler event."""
    return {
        "movie_id": movie_id,
        "produced": events.attributes_to_json(produced) if produced is not None else None,
        "raw_payload": raw_payload,
        "computed_pes": computed_pes,
        "expected_pes": expected_pes,
        "expected_sentiment": expected_sentiment.value,
        "expected_mood": expected_mood.value,
        "guardrail_attached": guardrail_attached,
    }


def dumps(payload: dict[str, Any]) -> str:
    """Encode a payload to JSON (asserts the event is JSON-safe)."""
    return json.dumps(payload)
