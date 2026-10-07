"""S3 Vectors + Titan embedder tests (stub/inject; no moto) — REQ-A-5.3/.5, REQ-X-3.3."""

from __future__ import annotations

import io
import json
from typing import Any

import boto3
from botocore.stub import Stubber

from movieintel.domain.schemas import Mood, Sentiment
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.vectors import (
    EmbedOutcome,
    TitanEmbedder,
    build_retrieval_metadata,
    embed_and_upsert,
)


def _titan_response(embedding: list[float]) -> dict[str, Any]:
    """Build a stubbed Titan V2 ``invoke_model`` response envelope."""
    body = json.dumps({"embedding": embedding, "inputTextTokenCount": 5}).encode("utf-8")
    return {
        "body": io.BytesIO(body),
        "contentType": "application/json",
    }


def test_titan_embedder_invokes_with_configured_model_id_and_body() -> None:
    """embed_overview calls invoke_model with the configured model id + {"inputText": text}."""
    config = PersistenceConfig.from_env(env={})
    client = boto3.client(
        "bedrock-runtime",
        region_name=config.region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    embedding = [0.1, 0.2, 0.3, 0.4]
    with Stubber(client) as stubber:
        stubber.add_response(
            "invoke_model",
            _titan_response(embedding),
            expected_params={
                "modelId": config.embedding_model_id,
                "accept": "application/json",
                "contentType": "application/json",
                "body": json.dumps({"inputText": "a dark thriller about testing"}),
            },
        )
        embedder = TitanEmbedder(client=client, config=config)
        result = embedder.embed_overview("a dark thriller about testing")
        stubber.assert_no_pending_responses()

    assert result == embedding


def test_titan_embedder_uses_non_default_model_id_from_config() -> None:
    """A custom embedding model id from config flows through (no inline literal)."""
    config = PersistenceConfig.from_env(
        env={"MOVIEINTEL_EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0-custom"}
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=config.region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "invoke_model",
            _titan_response([1.0, 2.0]),
            expected_params={
                "modelId": "amazon.titan-embed-text-v2:0-custom",
                "accept": "application/json",
                "contentType": "application/json",
                "body": json.dumps({"inputText": "x"}),
            },
        )
        embedder = TitanEmbedder(client=client, config=config)
        assert embedder.embed_overview("x") == [1.0, 2.0]
        stubber.assert_no_pending_responses()


def test_build_retrieval_metadata_has_expected_fields(enriched_record: Any) -> None:
    record = enriched_record(
        movie_id=42,
        pes_value=77.5,
        sentiment=Sentiment.POSITIVE,
        mood=Mood.UPLIFTING,
        title="The Example",
        genres='["Drama"]',
    )
    metadata = build_retrieval_metadata(record)
    assert metadata == {
        "title": "The Example",
        "genres": '["Drama"]',
        "sentiment": "positive",
        "mood": "uplifting",
        "pes": 77.5,
    }


def test_embed_and_upsert_passes_id_vector_and_metadata(
    enriched_record: Any, fake_embedder: Any, fake_writer: Any
) -> None:
    """embed_and_upsert gives the writer str(movieid), the vector, and the metadata."""
    record = enriched_record(
        movie_id=42,
        pes_value=77.5,
        sentiment=Sentiment.POSITIVE,
        mood=Mood.UPLIFTING,
        title="The Example",
        genres='["Drama"]',
        overview="A dark thriller.",
    )
    result = embed_and_upsert(record, embedder=fake_embedder, writer=fake_writer)

    assert result == EmbedOutcome(movie_id="42", embedded=True, skipped=False, reason=None)
    assert fake_embedder.texts == ["A dark thriller."]
    assert len(fake_writer.calls) == 1
    movie_id, vector, metadata = fake_writer.calls[0]
    assert movie_id == "42"
    assert vector == fake_embedder.vector
    assert metadata == {
        "title": "The Example",
        "genres": '["Drama"]',
        "sentiment": "positive",
        "mood": "uplifting",
        "pes": 77.5,
    }


def test_embed_and_upsert_is_idempotent_by_movie_id(
    enriched_record: Any, fake_embedder: Any, fake_writer: Any
) -> None:
    """Re-running keys the vector by the same movieid (upsert, not duplicate — REQ-A-5.5)."""
    record = enriched_record(movie_id=42, pes_value=77.5)
    embed_and_upsert(record, embedder=fake_embedder, writer=fake_writer)
    embed_and_upsert(record, embedder=fake_embedder, writer=fake_writer)

    keys = {call[0] for call in fake_writer.calls}
    assert keys == {"42"}  # both writes target the same key -> overwrite, no duplicate


def test_embed_and_upsert_skips_missing_overview(
    enriched_record: Any, fake_embedder: Any, fake_writer: Any
) -> None:
    """A missing (None) overview is skipped: no Titan call, no upsert, skipped outcome.

    Titan v2 rejects an empty inputText (minLength:1), so a blank overview (~2.1% of
    movies) is a tolerated per-item skip rather than an error (design §3.4, consistent
    with the pipeline per-item Catch design).
    """
    record = enriched_record(movie_id=1, pes_value=10.0, overview=None)
    result = embed_and_upsert(record, embedder=fake_embedder, writer=fake_writer)
    assert result == EmbedOutcome(
        movie_id="1", embedded=False, skipped=True, reason="empty-overview"
    )
    assert fake_embedder.texts == []
    assert fake_writer.calls == []


def test_embed_and_upsert_skips_whitespace_overview(
    enriched_record: Any, fake_embedder: Any, fake_writer: Any
) -> None:
    """A whitespace-only overview is skipped exactly like a blank one (no Titan call)."""
    record = enriched_record(movie_id=2, pes_value=10.0, overview="   \n\t")
    result = embed_and_upsert(record, embedder=fake_embedder, writer=fake_writer)
    assert result == EmbedOutcome(
        movie_id="2", embedded=False, skipped=True, reason="empty-overview"
    )
    assert fake_embedder.texts == []
    assert fake_writer.calls == []
