"""EmbedVector handler tests: embed the overview and upsert via injected fakes.

The handler accepts an injected :class:`Embedder` and :class:`VectorWriter` in tests
(reusing the ``FakeEmbedder``/``FakeVectorWriter`` patterns) and calls
``vectors.embed_and_upsert``. It duplicates no embedding logic. The test asserts the
upsert was recorded, keyed by the string movie id, with retrieval metadata.
"""

from __future__ import annotations

from typing import Any

from handlers.embed_vector import handler as embed_handler
from handlers.shared import events

from tests.handlers.conftest import FakeEmbedder, FakeVectorWriter, make_enriched, make_sampled


def _validate_output(enriched: Any) -> dict[str, Any]:
    return {
        "status": "enriched",
        "movie_id": enriched.movie.movie.movie_id,
        "outcome": "ok",
        "enriched": events.enriched_to_json(enriched),
    }


def test_embed_vector_records_upsert(
    fake_embedder: FakeEmbedder, fake_writer: FakeVectorWriter
) -> None:
    """The handler embeds the overview and records exactly one keyed upsert."""
    enriched = make_enriched(sampled=make_sampled(movie_id=555, overview="A moody thriller."))
    event = _validate_output(enriched)

    result = embed_handler.handler(event, None, embedder=fake_embedder, writer=fake_writer)

    assert result["status"] == "embedded"
    assert result["movie_id"] == 555

    assert fake_embedder.texts == ["A moody thriller."]
    assert len(fake_writer.calls) == 1
    movie_id, vector, metadata = fake_writer.calls[0]
    assert movie_id == "555"
    assert vector == fake_embedder.vector
    assert metadata["title"] == enriched.movie.movie.title


def test_embed_vector_skips_missing_overview(
    fake_embedder: FakeEmbedder, fake_writer: FakeVectorWriter
) -> None:
    """A missing overview is surfaced as an embed-skipped item: no Titan call, no upsert.

    Titan v2 rejects an empty inputText (minLength:1), so a blank overview is a tolerated
    per-item skip (REQ-A-6.2). The handler returns an ``embed-skipped`` envelope still
    carrying ``movie_id`` so Evaluate can key it.
    """
    enriched = make_enriched(sampled=make_sampled(movie_id=556, overview=None))
    event = _validate_output(enriched)

    result = embed_handler.handler(event, None, embedder=fake_embedder, writer=fake_writer)

    assert result["status"] == "embed-skipped"
    assert result["movie_id"] == 556
    assert result["outcome"] == "ok"
    assert result["enriched"] == event["enriched"]
    assert fake_embedder.texts == []
    assert fake_writer.calls == []


def test_embed_vector_skips_whitespace_overview(
    fake_embedder: FakeEmbedder, fake_writer: FakeVectorWriter
) -> None:
    """A whitespace-only overview is skipped exactly like a blank one."""
    enriched = make_enriched(sampled=make_sampled(movie_id=557, overview="   \n\t"))
    event = _validate_output(enriched)

    result = embed_handler.handler(event, None, embedder=fake_embedder, writer=fake_writer)

    assert result["status"] == "embed-skipped"
    assert result["movie_id"] == 557
    assert fake_embedder.texts == []
    assert fake_writer.calls == []
