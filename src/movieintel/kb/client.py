"""Bedrock Knowledge Base ``Retrieve`` client: semantic_search (design §3.9, REQ-B-1).

``semantic_search`` drives the design §3.9 flow for a natural-language query:

1. Build a ``bedrock-agent-runtime.retrieve`` request with the configured
   ``knowledgeBaseId`` (never inlined; from :class:`KBConfig`), the query text, and the
   result count (``top_k`` override or config default).
2. Call ``Retrieve`` (NOT ``RetrieveAndGenerate``) so orchestration/ranking stays in
   application code (REQ-B-1.2).
3. Parse ``retrievalResults`` into :class:`RetrievedMovie` records, passing the vector
   metadata written by :func:`movieintel.persistence.vectors.build_retrieval_metadata`
   (title/genres/sentiment/mood/pes) straight through. Missing or out-of-set metadata
   degrades to ``None`` rather than being fabricated; an empty index returns ``[]``.

Bedrock is reached through the :class:`BedrockAgentRuntimeClient` Protocol; boto3's
``bedrock-agent-runtime`` client satisfies it structurally, and tests drive a stub. The
tool interface is identical regardless of the backing vector store (S3 Vectors or the
OpenSearch fallback) because it depends only on the KB id and the Retrieve API, not on
the store (REQ-B-1.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from movieintel.domain.schemas import Mood, Sentiment
from movieintel.kb.config import KBConfig


class BedrockAgentRuntimeClient(Protocol):
    """Structural interface for the Bedrock ``Retrieve`` call (design §3.9)."""

    def retrieve(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RetrievedMovie:
    """One semantic-search hit: the chunk text/score plus the vector retrieval metadata.

    The metadata mirrors :func:`movieintel.persistence.vectors.build_retrieval_metadata`
    (title/genres/sentiment/mood/pes). Any field absent from the stored metadata, or
    carrying an out-of-set enum/non-numeric value, is ``None`` here - the client never
    fabricates a value the vector store did not provide.
    """

    text: str
    score: float | None
    title: str | None
    genres: str | None
    sentiment: Sentiment | None
    mood: Mood | None
    pes: float | None


def _coerce_enum(enum_cls: type[Sentiment] | type[Mood], raw: Any) -> Any:
    """Return the enum member for ``raw`` if it is a valid member, else ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        return enum_cls(raw)
    except ValueError:
        return None


def _coerce_float(raw: Any) -> float | None:
    """Return ``raw`` as a float when numeric-convertible, else ``None`` (no fabrication)."""
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_str(raw: Any) -> str | None:
    """Return ``raw`` when it is a string, else ``None``."""
    return raw if isinstance(raw, str) else None


def _to_retrieved_movie(result: dict[str, Any]) -> RetrievedMovie:
    """Map one Retrieve ``retrievalResults`` entry to a :class:`RetrievedMovie`."""
    content = result.get("content") or {}
    text = content.get("text") if isinstance(content, dict) else None
    metadata = result.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return RetrievedMovie(
        text=text if isinstance(text, str) else "",
        score=_coerce_float(result.get("score")),
        title=_coerce_str(metadata.get("title")),
        genres=_coerce_str(metadata.get("genres")),
        sentiment=_coerce_enum(Sentiment, metadata.get("sentiment")),
        mood=_coerce_enum(Mood, metadata.get("mood")),
        pes=_coerce_float(metadata.get("pes")),
    )


def semantic_search(
    query: str,
    *,
    client: BedrockAgentRuntimeClient,
    config: KBConfig,
    top_k: int | None = None,
) -> list[RetrievedMovie]:
    """Retrieve enriched movies semantically via the Bedrock KB Retrieve API (REQ-B-1.2).

    ``top_k`` overrides :attr:`KBConfig.number_of_results` when provided. Returns a list
    of :class:`RetrievedMovie` (empty when the index/query yields nothing - the Task 6
    index is currently EMPTY, which is expected). Orchestration/ranking stays in app
    code; this uses ``Retrieve``, never ``RetrieveAndGenerate``.

    :raises KnowledgeBaseNotConfiguredError: when no KB id is configured.
    """
    knowledge_base_id = config.require_knowledge_base_id()
    number_of_results = top_k if top_k is not None else config.number_of_results

    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": number_of_results}
        },
    )

    results = response.get("retrievalResults") or []
    return [_to_retrieved_movie(result) for result in results if isinstance(result, dict)]
