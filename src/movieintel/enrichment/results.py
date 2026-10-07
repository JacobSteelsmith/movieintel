"""Enrichment outcome types — a discriminated union of frozen dataclasses (design §3.3).

These are internal control-flow outcomes, not serialized LLM payloads, so they are
plain frozen dataclasses (mirroring ``data_access.models.ColumnInfo``) rather than
Pydantic models. ``Ok``/``Repaired`` carry a validated :class:`EnrichmentAttributes`;
``Failed``/``Blocked`` carry a human-readable ``reason``. ``enrich_movie`` never raises
for a per-movie problem — it returns one of these instead (REQ-A-4.3/.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from movieintel.domain.schemas import EnrichmentAttributes


@dataclass(frozen=True, slots=True)
class Ok:
    """Enrichment succeeded on the first attempt with valid structured output."""

    attributes: EnrichmentAttributes


@dataclass(frozen=True, slots=True)
class Repaired:
    """Enrichment succeeded after ``attempts`` bounded repair turns (REQ-A-4.3)."""

    attributes: EnrichmentAttributes
    attempts: int


@dataclass(frozen=True, slots=True)
class Failed:
    """Repair exhausted; the per-movie failure is recorded, not raised (REQ-A-4.3)."""

    reason: str


@dataclass(frozen=True, slots=True)
class Blocked:
    """A Guardrail intervened; no unsafe content is returned (REQ-A-4.5, REQ-X-4.3)."""

    reason: str


# The enrichment result is exactly one of these four outcomes.
type EnrichmentResult = Ok | Repaired | Failed | Blocked
