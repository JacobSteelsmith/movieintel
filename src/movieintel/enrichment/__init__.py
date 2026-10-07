"""Enrichment layer: Bedrock Converse client with structured output + repair + Guardrails.

Produces and validates :class:`EnrichmentAttributes` for a single movie, preserving the
deterministic PES passed in from the domain layer (REQ-A-4). Public API re-exported for
ergonomic imports, mirroring the other layers.
"""

from __future__ import annotations

from movieintel.enrichment.client import (
    MAX_REPAIR_ATTEMPTS,
    BedrockConverseClient,
    enrich_movie,
)
from movieintel.enrichment.results import Blocked, EnrichmentResult, Failed, Ok, Repaired

__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "BedrockConverseClient",
    "Blocked",
    "EnrichmentResult",
    "Failed",
    "Ok",
    "Repaired",
    "enrich_movie",
]
