"""Persistence layer: DynamoDB single-table repo + S3 Vectors writer + Titan embedder.

Design §3.4 / Task 5. Stores the enriched movie (source serving fields + the five
enrichment attributes + the PES components) in DynamoDB and the overview embedding in an
S3 Vectors index. Resource names and the embedding model id come from
:class:`PersistenceConfig`; the real table/bucket are created by CDK in Task 6.
"""

from __future__ import annotations

from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie
from movieintel.persistence.keys import (
    meta_sk,
    movie_pk,
    pes_gsi1sk,
    sentiment_gsi1pk,
)
from movieintel.persistence.repository import MovieIntelRepository
from movieintel.persistence.vectors import (
    Embedder,
    EmbedOutcome,
    S3VectorsWriter,
    TitanEmbedder,
    VectorWriter,
    build_retrieval_metadata,
    embed_and_upsert,
)

__all__ = [
    "EmbedOutcome",
    "Embedder",
    "EnrichedMovie",
    "MovieIntelRepository",
    "PersistenceConfig",
    "S3VectorsWriter",
    "TitanEmbedder",
    "VectorWriter",
    "build_retrieval_metadata",
    "embed_and_upsert",
    "meta_sk",
    "movie_pk",
    "pes_gsi1sk",
    "sentiment_gsi1pk",
]
