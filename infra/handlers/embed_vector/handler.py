"""EmbedAndUpsertVector state handler: embed the overview, upsert the vector (Task 6).

Thin wrapper over :func:`movieintel.persistence.vectors.embed_and_upsert`; it duplicates
no embedding or upsert logic. In production it builds a :class:`TitanEmbedder` and
:class:`S3VectorsWriter` from boto3 clients named by :class:`PersistenceConfig`. Tests
inject an :class:`Embedder`/:class:`VectorWriter` directly (fakes), so no AWS is touched
(REQ-X-3.4). The vector is keyed by the string movie id so re-runs overwrite (REQ-A-5.5).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.vectors import (
    Embedder,
    S3VectorsWriter,
    TitanEmbedder,
    VectorWriter,
    embed_and_upsert,
)


def _default_embedder(config: PersistenceConfig) -> Embedder:
    """Build the live Titan embedder from the configured region (lazy boto3 import)."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=config.region)
    return TitanEmbedder(client=client, config=config)


def _default_writer(config: PersistenceConfig) -> VectorWriter:
    """Build the live S3 Vectors writer from the configured region (lazy boto3 import)."""
    import boto3

    client = boto3.client("s3vectors", region_name=config.region)
    return S3VectorsWriter(client=client, config=config)


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    embedder: Embedder | None = None,
    writer: VectorWriter | None = None,
    config: PersistenceConfig | None = None,
) -> JsonDict:
    """Embed the overview of ``event["enriched"]`` and upsert the vector."""
    config = config or PersistenceConfig.from_env()
    embedder = embedder if embedder is not None else _default_embedder(config)
    writer = writer if writer is not None else _default_writer(config)

    enriched = events.enriched_from_json(event["enriched"])
    outcome = embed_and_upsert(enriched, embedder=embedder, writer=writer)

    # A blank overview is a tolerated per-item skip (Titan v2 rejects an empty inputText);
    # surface it as ``embed-skipped`` - distinct from an upstream failed/blocked ``skipped``
    # so the two causes stay distinguishable in logs/metrics - while still routing through
    # the Evaluate skip path. The envelope keeps ``movie_id`` so Evaluate can key it
    # (REQ-A-6.2). ``enriched`` is still carried so the shape matches the embedded path.
    status = "embedded" if outcome.embedded else "embed-skipped"

    # Pass ``enriched`` (and ``outcome``) through so this terminal item-chain state leaves
    # the Map result in the shape the Evaluate state consumes: an item from which Evaluate
    # reconstructs the EnrichedMovie via ``event["enriched"]`` (REQ-A-6.3).
    return {
        "status": status,
        "movie_id": enriched.movie.movie.movie_id,
        "outcome": event.get("outcome", "ok"),
        "enriched": event["enriched"],
    }
