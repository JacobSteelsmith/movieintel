"""The five golden-set metric computations (REQ-X-2.2), pure over ``list[EvalRecord]``.

Each function returns a :class:`MetricResult`. Documented behavior: records that FAIL
schema validation (``produced is None``) are excluded from the label-based metrics
(sentiment, mood) because there are no produced labels to compare — but they are still
counted in the schema-validation rate and in guardrail coverage, which observe every
evaluated record. Exactness and coverage must be 1.0 to pass (REQ-X-2.5, REQ-X-4.4).
"""

from __future__ import annotations

from collections.abc import Sequence

from movieintel.eval.models import EvalRecord, MetricResult, SentimentJudge


def _rate(numerator: int, denominator: int) -> float:
    """Fraction in ``[0, 1]``; an empty sample is a vacuous 1.0 (nothing failed)."""
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _validated(records: Sequence[EvalRecord]) -> list[EvalRecord]:
    """Records whose enrichment output passed schema validation (``produced`` set)."""
    return [r for r in records if r.produced is not None]


def schema_validation_rate(records: Sequence[EvalRecord]) -> MetricResult:
    """Fraction of evaluated records whose output passed ``EnrichmentAttributes``."""
    total = len(records)
    valid = len(_validated(records))
    rate = _rate(valid, total)
    return MetricResult(
        name="schema_validation",
        rate=rate,
        passed=rate == 1.0,
        detail=f"{valid}/{total} outputs passed schema validation",
    )


def pes_exactness(records: Sequence[EvalRecord]) -> MetricResult:
    """Fraction where computed PES equals the independent expected PES (REQ-X-2.5).

    Must be 100% or the check fails — the deterministic score has to be exact.
    """
    total = len(records)
    exact = sum(1 for r in records if r.computed_pes == r.expected_pes)
    rate = _rate(exact, total)
    return MetricResult(
        name="pes_exactness",
        rate=rate,
        passed=rate == 1.0,
        detail=f"{exact}/{total} computed PES values matched the expected literal",
    )


def sentiment_agreement(records: Sequence[EvalRecord], judge: SentimentJudge) -> MetricResult:
    """LLM-as-judge agreement between produced and golden sentiment (design §3.5).

    Computed only over schema-valid records; the injected ``judge`` decides agreement.
    """
    validated = _validated(records)
    total = len(validated)
    agreed = 0
    for record in validated:
        assert record.produced is not None  # guaranteed by _validated
        if judge.agree(record.produced.overview_sentiment, record.expected_sentiment):
            agreed += 1
    rate = _rate(agreed, total)
    return MetricResult(
        name="sentiment_agreement",
        rate=rate,
        passed=rate == 1.0,
        detail=f"{agreed}/{total} produced sentiments agreed with the golden label",
    )


def mood_agreement(records: Sequence[EvalRecord]) -> MetricResult:
    """Direct label-match of produced vs. golden mood — deterministic, no judge (§3.5).

    Mood is a bounded ``Mood`` enum, so agreement is a crisp ``==`` comparison; the
    sentiment judge is never consulted for mood.
    """
    validated = _validated(records)
    total = len(validated)
    matched = 0
    for record in validated:
        assert record.produced is not None  # guaranteed by _validated
        if record.produced.mood == record.expected_mood:
            matched += 1
    rate = _rate(matched, total)
    return MetricResult(
        name="mood_agreement",
        rate=rate,
        passed=rate == 1.0,
        detail=f"{matched}/{total} produced moods matched the golden label",
    )


def guardrail_coverage(records: Sequence[EvalRecord]) -> MetricResult:
    """Fraction of evaluated records with a Guardrail attached (REQ-X-4.4).

    Must be 100% — every Converse call must attach a Guardrail.
    """
    total = len(records)
    covered = sum(1 for r in records if r.guardrail_attached)
    rate = _rate(covered, total)
    return MetricResult(
        name="guardrail_coverage",
        rate=rate,
        passed=rate == 1.0,
        detail=f"{covered}/{total} evaluated calls had a Guardrail attached",
    )
