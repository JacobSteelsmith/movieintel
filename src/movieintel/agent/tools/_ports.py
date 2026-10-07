"""Injection Protocols for the agent tools (design §3.6).

The tools depend on narrow structural interfaces, not on concrete classes, mirroring the
``enrichment.client.BedrockConverseClient`` and ``persistence.vectors`` precedent. This
keeps each tool unit-testable with a moto-backed real repository, a botocore-stubbed KB
client, or a hand stub, and lets production inject the real
:class:`movieintel.persistence.repository.MovieIntelRepository` / boto3
``bedrock-agent-runtime`` client (both satisfy these Protocols structurally).

The KB port is reused from :mod:`movieintel.kb.client` (``BedrockAgentRuntimeClient``) —
it is imported and re-exported here, never redefined.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from movieintel.domain.schemas import Sentiment
from movieintel.kb.client import BedrockAgentRuntimeClient
from movieintel.persistence.item import EnrichedMovie

__all__ = ["BedrockAgentRuntimeClient", "MovieRepository"]


class MovieRepository(Protocol):
    """Structural interface over the repository methods the tools call (design §3.4).

    :class:`movieintel.persistence.repository.MovieIntelRepository` conforms to this
    structurally, so production injects the real repository and tests inject either a
    moto-backed real repository or a hand stub.
    """

    def query_by_sentiment(
        self, sentiment: Sentiment, *, limit: int | None = ...
    ) -> list[EnrichedMovie]: ...

    def scan_numeric_range(
        self,
        *,
        min_budget: float | None = ...,
        max_budget: float | None = ...,
        min_revenue: float | None = ...,
        max_revenue: float | None = ...,
        min_runtime: float | None = ...,
        max_runtime: float | None = ...,
    ) -> list[EnrichedMovie]: ...

    def batch_get(self, movie_ids: Sequence[int | str]) -> list[EnrichedMovie]: ...
