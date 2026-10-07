"""Eval records, the injectable judge Protocol, and metric value types (REQ-X-2).

The harness consumes pre-produced *evaluation records* — one per golden item — rather
than calling the Task 3 enrichment client itself (that keeps Task 3's behavior untouched
and lets the schema-validation-rate and guardrail-coverage metrics observe both passing
and failing records deterministically). The pipeline (Task 6) will build these records
from real enrichment outputs; Task 4 defines the record type and the metrics over it.

Types here reuse the real domain schemas (``Sentiment``, ``Mood``, ``EnrichmentAttributes``)
rather than redefining them, so the harness evaluates production types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from movieintel.domain.schemas import EnrichmentAttributes, Mood, Sentiment


class GoldenItem(BaseModel):
    """One hand-labeled golden fixture item (REQ-X-2.1).

    Carries the movie inputs needed to recompute PES independently plus the expected
    labels. Strict (``extra="forbid"``) so a malformed fixture rejects loudly.
    """

    model_config = ConfigDict(extra="forbid")

    movie_id: int
    title: str
    overview: str
    budget: int | None
    revenue: int | None
    genres: str | None = None
    language: str | None = None
    release_date: str | None = None
    runtime: float | None = None
    rating: float | None
    rating_count: int = 0
    effective_max: float
    expected_sentiment: Sentiment
    expected_mood: Mood
    expected_pes: float


class EvalRecord(BaseModel):
    """A single evaluation input: one produced enrichment outcome vs. its golden labels.

    ``produced`` is the validated :class:`EnrichmentAttributes` when the enrichment
    output passed Pydantic validation, else ``None`` with the pre-validation
    ``raw_payload`` present so the schema-validation-rate metric can count the failure.
    ``computed_pes`` is the production PES fed in as the subject-under-test; it is
    compared against the independently-derived ``expected_pes`` (REQ-X-2.5).
    ``guardrail_attached`` records whether a Guardrail was attached to the Converse call
    that produced this record (REQ-X-4.4).
    """

    model_config = ConfigDict(extra="forbid")

    movie_id: int
    produced: EnrichmentAttributes | None
    raw_payload: dict[str, Any] | None = None
    computed_pes: float
    expected_pes: float
    expected_sentiment: Sentiment
    expected_mood: Mood
    guardrail_attached: bool


@runtime_checkable
class SentimentJudge(Protocol):
    """Injectable LLM-as-judge for sentiment agreement (design §3.5).

    CI passes :class:`DeterministicSentimentJudge`; the live path wraps Bedrock
    (``eval.judge.BedrockSentimentJudge``). Mirrors the ``BedrockConverseClient``
    Protocol-injection pattern in ``enrichment.client`` to keep CI deterministic/free.
    """

    def agree(self, produced: Sentiment, expected: Sentiment) -> bool: ...


class DeterministicSentimentJudge:
    """Deterministic CI judge: agreement is exact label equality (no Bedrock)."""

    def agree(self, produced: Sentiment, expected: Sentiment) -> bool:
        return produced == expected


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One metric outcome: a fraction in ``[0, 1]`` plus a pass flag and a note.

    Frozen value type (mirrors ``data_access.models.ColumnInfo``). ``passed`` is the
    metric's own gate (e.g. PES exactness and guardrail coverage must be 1.0).
    """

    name: str
    rate: float
    passed: bool
    detail: str
