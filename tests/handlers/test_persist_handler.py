"""Persist handler tests: write to a moto MovieIntel table via the repository.

The handler builds a :class:`PersistenceConfig`, accepts an injected DynamoDB ``Table``
in tests (moto), and upserts an :class:`EnrichedMovie` through
:class:`MovieIntelRepository`. It duplicates no persistence logic. The round-trip is
asserted by reading the item back out of the table.
"""

from __future__ import annotations

from typing import Any

from handlers.embed_vector import handler as embed_handler
from handlers.persist import handler as persist_handler
from handlers.shared import events

from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.repository import MovieIntelRepository
from tests.handlers.conftest import FakeEmbedder, FakeVectorWriter, make_enriched, make_sampled


def _validate_output(enriched: Any) -> dict[str, Any]:
    """Assemble the Validate-state output the Persist handler consumes."""
    return {
        "status": "enriched",
        "movie_id": enriched.movie.movie.movie_id,
        "outcome": "ok",
        "enriched": events.enriched_to_json(enriched),
    }


def test_persist_writes_and_reads_back(
    dynamodb_table: Any, persistence_config: PersistenceConfig
) -> None:
    """The handler writes the record; the repository reads back the same movie."""
    enriched = make_enriched(sampled=make_sampled(movie_id=321))
    event = _validate_output(enriched)

    result = persist_handler.handler(event, None, table=dynamodb_table)

    assert result["status"] == "persisted"
    assert result["movie_id"] == 321

    repo = MovieIntelRepository(table=dynamodb_table, config=persistence_config)
    stored = repo.get(321)
    assert stored is not None
    assert stored.movie.movie.movie_id == 321


def test_persist_is_idempotent(dynamodb_table: Any, persistence_config: PersistenceConfig) -> None:
    """Re-running the handler overwrites rather than duplicates (REQ-A-5.5)."""
    enriched = make_enriched(sampled=make_sampled(movie_id=77))
    event = _validate_output(enriched)

    persist_handler.handler(event, None, table=dynamodb_table)
    persist_handler.handler(event, None, table=dynamodb_table)

    repo = MovieIntelRepository(table=dynamodb_table, config=persistence_config)
    assert repo.get(77) is not None


def test_persist_output_feeds_embed_vector(
    dynamodb_table: Any,
    fake_embedder: FakeEmbedder,
    fake_writer: FakeVectorWriter,
) -> None:
    """Persist's output is a valid EmbedVector input (the chained seam, REQ-A-6.1).

    The state machine chains PersistDynamoDB -> EmbedAndUpsertVector with
    ``payload_response_only``, so EmbedVector sees only Persist's return value. Persist
    must pass ``enriched`` through or the downstream embed would fail on a missing key.
    """
    enriched = make_enriched(sampled=make_sampled(movie_id=999))
    event = _validate_output(enriched)

    persist_output = persist_handler.handler(event, None, table=dynamodb_table)

    assert "enriched" in persist_output
    assert persist_output["outcome"] == "ok"
    embed_output = embed_handler.handler(
        persist_output, None, embedder=fake_embedder, writer=fake_writer
    )
    assert embed_output["status"] == "embedded"
    assert embed_output["movie_id"] == 999
    assert len(fake_writer.calls) == 1
    # The embed output is the Map item result Evaluate consumes: it still carries the
    # enriched record and outcome so Evaluate can reconstruct the EnrichedMovie.
    assert "enriched" in embed_output
    assert embed_output["outcome"] == "ok"
