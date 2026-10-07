"""Fixtures for agent-tool tests: moto repository + stub repo + KB stub (design §3.6).

Reuses the persistence ``dynamodb_table`` + ``make_enriched`` builder (imported from
``tests/persistence/conftest.py``) so the tools are exercised against a faithful moto
table through a real :class:`MovieIntelRepository`. ``StubRepository`` records every call
for the no-execution assertions (REQ-B-2.5), and ``FakeAgentRuntime`` is the KB
``retrieve`` stub (same pattern as ``tests/kb/test_semantic_search.py``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import boto3
import pytest
from moto import mock_aws

from movieintel.domain.schemas import Sentiment
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie
from movieintel.persistence.repository import MovieIntelRepository
from tests.persistence.conftest import make_enriched

__all__ = ["make_enriched"]


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


@pytest.fixture
def repository(dynamodb_table: Any, config: PersistenceConfig) -> MovieIntelRepository:
    """A real repository wrapping the moto table (production class, mocked backend)."""
    return MovieIntelRepository(table=dynamodb_table, config=config)


class StubRepository:
    """Hand stub recording every call, for no-execution + pure-logic assertions.

    Pre-load ``by_sentiment`` / ``scan_result`` / ``batch`` with the records a given test
    needs. ``calls`` records the method name and kwargs so a test can assert the tool did
    (or, for invalid args, did NOT) touch the repository.
    """

    def __init__(
        self,
        *,
        by_sentiment: list[EnrichedMovie] | None = None,
        scan_result: list[EnrichedMovie] | None = None,
        batch: list[EnrichedMovie] | None = None,
    ) -> None:
        self._by_sentiment = by_sentiment or []
        self._scan_result = scan_result or []
        self._batch = batch or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query_by_sentiment(
        self, sentiment: Sentiment, *, limit: int | None = None
    ) -> list[EnrichedMovie]:
        self.calls.append(("query_by_sentiment", {"sentiment": sentiment, "limit": limit}))
        return list(self._by_sentiment)

    def scan_numeric_range(
        self,
        *,
        min_budget: float | None = None,
        max_budget: float | None = None,
        min_revenue: float | None = None,
        max_revenue: float | None = None,
        min_runtime: float | None = None,
        max_runtime: float | None = None,
    ) -> list[EnrichedMovie]:
        self.calls.append(
            (
                "scan_numeric_range",
                {
                    "min_budget": min_budget,
                    "max_budget": max_budget,
                    "min_revenue": min_revenue,
                    "max_revenue": max_revenue,
                    "min_runtime": min_runtime,
                    "max_runtime": max_runtime,
                },
            )
        )
        return list(self._scan_result)

    def batch_get(self, movie_ids: Sequence[int | str]) -> list[EnrichedMovie]:
        self.calls.append(("batch_get", {"movie_ids": list(movie_ids)}))
        return list(self._batch)


class FakeAgentRuntime:
    """Capture ``retrieve`` kwargs and return a canned response (KB client stub)."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response if response is not None else {"retrievalResults": []}
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response
