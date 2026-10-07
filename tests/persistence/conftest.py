"""Fixtures for persistence tests: moto DynamoDB table + record builder + fakes.

The ``dynamodb_table`` fixture creates a ``MovieIntel`` table under moto with the SAME key
schema the design specifies (PK/SK plus GSI1 on GSI1PK/GSI1SK), so the repository code is
exercised against a faithful table. In production the table is created by CDK (Task 6).

``enriched_record`` builds an :class:`EnrichedMovie` from the real domain types with
controllable sentiment/PES/budget/revenue/runtime, so individual tests can target a single
access pattern. ``FakeEmbedder``/``FakeVectorWriter`` stand in for the S3 Vectors + Titan
Protocols (no moto coverage for those).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from movieintel.data_access.models import MovieRow, SampledMovie
from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
    TierOrUnknown,
)
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie


@pytest.fixture
def config() -> PersistenceConfig:
    """Default persistence config (table ``MovieIntel``, GSI ``GSI1``)."""
    return PersistenceConfig.from_env(env={})


@pytest.fixture
def dynamodb_table(config: PersistenceConfig) -> Iterator[Any]:
    """A moto-backed ``MovieIntel`` table with the design key schema + GSI1."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=config.region)
        table = resource.create_table(
            TableName=config.table_name,
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
                    "IndexName": config.gsi1_name,
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


def make_enriched(
    *,
    movie_id: int,
    pes_value: float,
    sentiment: Sentiment = Sentiment.POSITIVE,
    title: str = "A Film",
    budget: int | None = 1_000_000,
    revenue: int | None = 5_000_000,
    runtime: float | None = 120.0,
    rating: float | None = 4.0,
    mood: Mood = Mood.UPLIFTING,
    budget_tier: TierOrUnknown = "unknown",
    revenue_tier: TierOrUnknown = "unknown",
    pes_tier: PESTier | None = PESTier.SOLID,
    overview: str | None = "An overview.",
    genres: str | None = '["Drama"]',
) -> EnrichedMovie:
    """Build an :class:`EnrichedMovie` from the real domain types (builder for tests)."""
    movie = SampledMovie(
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
        rating_count=10,
    )
    attributes = EnrichmentAttributes(
        overview_sentiment=sentiment,
        budget_tier=budget_tier,
        revenue_tier=revenue_tier,
        effectiveness=EffectivenessScore(
            value=pes_value,
            roi_defined=budget is not None and budget > 0,
            rating_defined=rating is not None,
            tier=pes_tier,
            explanation="Because reasons.",
        ),
        mood=mood,
        reasoning_basis="Derived from the overview and the numbers.",
    )
    return EnrichedMovie(movie=movie, attributes=attributes)


@pytest.fixture
def enriched_record() -> Any:
    """Expose the :func:`make_enriched` builder to tests."""
    return make_enriched


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
