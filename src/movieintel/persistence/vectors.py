"""S3 Vectors writer + Titan embedder behind Protocols (design §3.4, §7.6; REQ-A-5.3/.5).

Mirrors the ``enrichment.client.BedrockConverseClient`` Protocol-injection pattern: the
embedder and vector writer are structural Protocols, so CI injects fakes/stubs and never
touches real AWS, while the real-boto3 implementations serve the live path and Task 6.
S3 Vectors has no moto coverage, so only the Protocol boundary is exercised in CI.

- :class:`TitanEmbedder` calls ``bedrock-runtime.invoke_model`` for Amazon Titan Text
  Embeddings V2. The model id comes from :class:`PersistenceConfig` (never inline). The
  request body is ``{"inputText": text}`` and the response body carries ``embedding``.
- :class:`S3VectorsWriter` upserts a vector into the configured S3 Vectors bucket/index,
  keyed by movieid so re-runs overwrite rather than duplicate (REQ-A-5.5).
- :func:`embed_and_upsert` embeds the overview and upserts the vector with retrieval
  metadata (title, genres, sentiment, mood, pes) per design §3.4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie


@dataclass(frozen=True)
class EmbedOutcome:
    """Result of :func:`embed_and_upsert`: embedded, or skipped as a tolerated outcome.

    ``skipped`` is set when the overview is empty/blank: Titan v2 rejects an empty
    ``inputText`` (minLength:1), so a blank overview (~2.1% of movies) is a tolerated
    per-item skip rather than an error (design §3.4, consistent with the pipeline
    per-item Catch design). ``reason`` records why the item was skipped (``None`` when
    embedded).
    """

    movie_id: str
    embedded: bool
    skipped: bool
    reason: str | None


class Embedder(Protocol):
    """Structural interface for turning overview text into an embedding vector."""

    def embed_overview(self, text: str) -> list[float]: ...


class VectorWriter(Protocol):
    """Structural interface for upserting a vector keyed by movieid (design §3.4)."""

    def upsert_vector(
        self, movie_id: str, vector: list[float], metadata: dict[str, Any]
    ) -> None: ...


class TitanEmbedder:
    """Real embedder: Amazon Titan Text Embeddings V2 via ``bedrock-runtime``.

    The ``bedrock-runtime`` client is injected (satisfied structurally by boto3). The
    model id is read from ``config.embedding_model_id`` — never inlined (REQ-A-5.3). The
    Titan V2 request body is ``{"inputText": text}``; the response body is
    ``{"embedding": [...], "inputTextTokenCount": <int>}``.
    """

    def __init__(self, *, client: Any, config: PersistenceConfig) -> None:
        self._client = client
        self._config = config

    def embed_overview(self, text: str) -> list[float]:
        """Embed ``text`` and return the Titan V2 embedding vector."""
        response = self._client.invoke_model(
            modelId=self._config.embedding_model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps({"inputText": text}),
        )
        payload = json.loads(response["body"].read())
        return [float(x) for x in payload["embedding"]]


class S3VectorsWriter:
    """Real writer: upsert a vector into an S3 Vectors index via the ``s3vectors`` client.

    Bucket/index names come from :class:`PersistenceConfig`. ``put_vectors`` is an upsert
    keyed by the vector key, so writing the same movieid twice overwrites rather than
    duplicates (REQ-A-5.5). The ``s3vectors`` client is injected; it is never created at
    import time, so this module imports safely even where ``s3vectors`` is unavailable.
    """

    def __init__(self, *, client: Any, config: PersistenceConfig) -> None:
        self._client = client
        self._config = config

    def upsert_vector(self, movie_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        """Upsert one vector (keyed by ``movie_id``) with retrieval metadata."""
        self._client.put_vectors(
            vectorBucketName=self._config.vector_bucket_name,
            indexName=self._config.vector_index_name,
            vectors=[
                {
                    "key": movie_id,
                    "data": {"float32": vector},
                    "metadata": metadata,
                }
            ],
        )


def build_retrieval_metadata(record: EnrichedMovie) -> dict[str, Any]:
    """Build the vector retrieval metadata (title, genres, sentiment, mood, pes).

    Design §3.4: metadata lets the agent surface titles and filter without a second
    DynamoDB round-trip. ``genres`` is stored as the raw column text (``None`` when
    missing — never fabricated).
    """
    row = record.movie.movie
    attrs = record.attributes
    return {
        "title": row.title,
        "genres": row.genres,
        "sentiment": attrs.overview_sentiment.value,
        "mood": attrs.mood.value,
        "pes": attrs.effectiveness.value,
    }


def embed_and_upsert(
    record: EnrichedMovie, *, embedder: Embedder, writer: VectorWriter
) -> EmbedOutcome:
    """Embed the overview and upsert the vector keyed by movieid (design §3.4).

    The overview text is ``record.movie.movie.overview``. A missing or blank overview is
    SKIPPED rather than embedded: Titan v2 rejects an empty ``inputText`` (minLength:1),
    so a blank overview (~2.1% of movies) is a tolerated per-item skip, not an error
    (design §3.4, consistent with the pipeline per-item Catch design, REQ-A-6.2). When a
    non-blank overview is present, the upsert is keyed by the string movieid, matching the
    DynamoDB key so both stores agree and re-runs overwrite rather than duplicate
    (REQ-A-5.5).

    Returns an :class:`EmbedOutcome` so callers can tell embedded from skipped without
    inspecting the vector store.
    """
    overview = (record.movie.movie.overview or "").strip()
    if not overview:
        return EmbedOutcome(
            movie_id=record.movie_id, embedded=False, skipped=True, reason="empty-overview"
        )
    vector = embedder.embed_overview(overview)
    writer.upsert_vector(record.movie_id, vector, build_retrieval_metadata(record))
    return EmbedOutcome(movie_id=record.movie_id, embedded=True, skipped=False, reason=None)
