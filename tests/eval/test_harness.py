"""Eval-harness tests — deterministic, mocked, over the committed golden set (REQ-X-2).

Every test here runs with a deterministic stub judge (no Bedrock) against the committed
``tests/fixtures/golden/golden_set.json`` fixtures, so the suite is free and reproducible
in CI (REQ-X-2.3, REQ-X-3.4). The expected-PES independence test (REQ-X-2.5) cross-checks
the committed literals against the independent reference implementation, never the
production PES function.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from movieintel.domain.schemas import EnrichmentAttributes, Mood, Sentiment
from movieintel.eval import (
    DeterministicSentimentJudge,
    EvalRecord,
    EvalReport,
    assert_exact,
    evaluate,
    load_golden,
)
from movieintel.eval.models import GoldenItem
from tests.domain._reference_pes import reference_pes_value

_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "golden_set.json"

_METRIC_FIELDS = (
    "schema_validation",
    "pes_exactness",
    "sentiment_agreement",
    "mood_agreement",
    "guardrail_coverage",
)


# --------------------------------------------------------------------------- helpers


def _attributes_for(item: GoldenItem) -> EnrichmentAttributes:
    """Build a schema-valid EnrichmentAttributes whose labels match the golden item."""
    return EnrichmentAttributes(
        overview_sentiment=item.expected_sentiment,
        budget_tier="medium",
        revenue_tier="high",
        effectiveness={
            "value": item.expected_pes,
            "roi_defined": True,
            "rating_defined": item.rating is not None,
        },
        mood=item.expected_mood,
        reasoning_basis="matches golden label",
    )


def _matching_record(item: GoldenItem) -> EvalRecord:
    """An eval record whose produced labels + computed PES all match the golden item."""
    return EvalRecord(
        movie_id=item.movie_id,
        produced=_attributes_for(item),
        raw_payload=None,
        computed_pes=item.expected_pes,
        expected_pes=item.expected_pes,
        expected_sentiment=item.expected_sentiment,
        expected_mood=item.expected_mood,
        guardrail_attached=True,
    )


def _matching_records() -> list[EvalRecord]:
    return [_matching_record(item) for item in load_golden(_GOLDEN_PATH)]


# ------------------------------------------------------------------- fixture loading


def test_load_golden_returns_typed_items() -> None:
    items = load_golden(_GOLDEN_PATH)
    assert len(items) >= 6
    for item in items:
        assert isinstance(item, GoldenItem)
        assert isinstance(item.expected_sentiment, Sentiment)
        assert isinstance(item.expected_mood, Mood)
        assert isinstance(item.expected_pes, float)


# ---------------------------------------------- deterministic end-to-end + 5 metrics


def test_harness_runs_deterministically_and_reports_five_metrics() -> None:
    records = _matching_records()
    judge = DeterministicSentimentJudge()

    first = evaluate(records, judge=judge)
    second = evaluate(records, judge=judge)

    assert isinstance(first, EvalReport)
    for field in _METRIC_FIELDS:
        assert hasattr(first, field)
    # Two runs are byte-for-byte identical (deterministic).
    assert first.model_dump() == second.model_dump()
    # On matching fixtures every metric is perfect.
    assert first.schema_validation.rate == 1.0
    assert first.pes_exactness.rate == 1.0
    assert first.sentiment_agreement.rate == 1.0
    assert first.mood_agreement.rate == 1.0
    assert first.guardrail_coverage.rate == 1.0


# ---------------------------------------------------------------- PES exactness gate


def test_pes_exactness_100_percent_on_correct_fixtures() -> None:
    report = evaluate(_matching_records(), judge=DeterministicSentimentJudge())
    assert report.pes_exactness.rate == 1.0
    assert report.pes_exactness.passed is True
    assert_exact(report)  # does not raise


def test_pes_exactness_below_100_fails_check() -> None:
    records = _matching_records()
    # One record's computed PES disagrees with its (deliberately-wrong) expected value.
    records[0] = records[0].model_copy(update={"computed_pes": records[0].expected_pes + 1.0})
    report = evaluate(records, judge=DeterministicSentimentJudge())

    assert report.pes_exactness.rate < 1.0
    assert report.pes_exactness.passed is False
    with pytest.raises(AssertionError):
        assert_exact(report)


# ------------------------------------------------------------ mood direct-label match


def test_mood_direct_label_match_counts_mismatches() -> None:
    records = _matching_records()
    original = records[0].produced
    assert original is not None
    flipped_mood = next(m for m in Mood if m != original.mood)
    records[0] = records[0].model_copy(
        update={"produced": original.model_copy(update={"mood": flipped_mood})}
    )

    report = evaluate(records, judge=DeterministicSentimentJudge())

    n = len(records)
    assert report.mood_agreement.rate == pytest.approx((n - 1) / n)


def test_mood_match_never_calls_the_judge() -> None:
    class _RecordingJudge:
        def __init__(self) -> None:
            self.calls: list[tuple[Sentiment, Sentiment]] = []

        def agree(self, produced: Sentiment, expected: Sentiment) -> bool:
            self.calls.append((produced, expected))
            return produced == expected

    judge = _RecordingJudge()
    records = _matching_records()
    # Flip a mood; the judge must still only ever see sentiment pairs, never mood.
    original = records[0].produced
    assert original is not None
    flipped = next(m for m in Mood if m != original.mood)
    records[0] = records[0].model_copy(
        update={"produced": original.model_copy(update={"mood": flipped})}
    )

    evaluate(records, judge=judge)

    # One call per record with produced attributes; all are sentiment pairs.
    assert len(judge.calls) == len(records)


# -------------------------------------------------- sentiment agreement via judge stub


def test_sentiment_agreement_uses_injected_judge_stub() -> None:
    class _ScriptedJudge:
        """Returns a scripted verdict per call and records the pairs it received."""

        def __init__(self, verdicts: list[bool]) -> None:
            self._verdicts = list(verdicts)
            self.pairs: list[tuple[Sentiment, Sentiment]] = []

        def agree(self, produced: Sentiment, expected: Sentiment) -> bool:
            self.pairs.append((produced, expected))
            return self._verdicts.pop(0)

    records = _matching_records()
    # Script: all agree except the first -> rate = (n-1)/n.
    verdicts = [False] + [True] * (len(records) - 1)
    judge = _ScriptedJudge(verdicts)

    report = evaluate(records, judge=judge)

    n = len(records)
    assert report.sentiment_agreement.rate == pytest.approx((n - 1) / n)
    # The judge saw exactly one (produced, expected) pair per record.
    assert len(judge.pairs) == n
    for record, (produced, expected) in zip(records, judge.pairs, strict=True):
        assert record.produced is not None
        assert produced == record.produced.overview_sentiment
        assert expected == record.expected_sentiment


# --------------------------------------------------------------- schema-validation rate


def test_schema_validation_rate_counts_invalid_payloads() -> None:
    records = _matching_records()
    bad_payload = {
        "overview_sentiment": "ecstatic",  # not in the Sentiment enum
        "budget_tier": "medium",
        "revenue_tier": "high",
        "mood": "uplifting",
        "reasoning_basis": "invalid",
    }
    records.append(
        EvalRecord(
            movie_id=999,
            produced=None,
            raw_payload=bad_payload,
            computed_pes=50.0,
            expected_pes=50.0,
            expected_sentiment=Sentiment.POSITIVE,
            expected_mood=Mood.UPLIFTING,
            guardrail_attached=True,
        )
    )

    report = evaluate(records, judge=DeterministicSentimentJudge())

    n = len(records)
    assert report.schema_validation.rate == pytest.approx((n - 1) / n)
    # The invalid record is excluded from label metrics (documented behavior): the
    # label metrics are computed over the n-1 valid records and remain perfect.
    assert report.sentiment_agreement.rate == 1.0
    assert report.mood_agreement.rate == 1.0
    # ...but PES exactness and guardrail coverage count every record.
    assert report.pes_exactness.rate == 1.0
    assert report.guardrail_coverage.rate == 1.0


# ----------------------------------------------------------------- guardrail coverage


def test_guardrail_coverage_full_vs_missing() -> None:
    full = _matching_records()
    report_full = evaluate(full, judge=DeterministicSentimentJudge())
    assert report_full.guardrail_coverage.rate == 1.0
    assert report_full.guardrail_coverage.passed is True

    missing = _matching_records()
    missing[0] = missing[0].model_copy(update={"guardrail_attached": False})
    report_missing = evaluate(missing, judge=DeterministicSentimentJudge())
    assert report_missing.guardrail_coverage.rate < 1.0
    assert report_missing.guardrail_coverage.passed is False


# ----------------------------------------------------- serializable metrics artifact


def test_metrics_artifact_serializable_json_and_table() -> None:
    report = evaluate(_matching_records(), judge=DeterministicSentimentJudge())

    dumped = report.model_dump()
    round_tripped = json.loads(json.dumps(dumped))
    assert set(round_tripped) >= set(_METRIC_FIELDS)

    table = report.render_table()
    assert isinstance(table, str)
    for label in _METRIC_FIELDS:
        assert label in table


# ---------------------------------------------- expected-PES independence (REQ-X-2.5)


def test_expected_pes_literals_are_independent() -> None:
    """Each committed expected_pes equals the INDEPENDENT reference calc, not prod PES."""
    for item in load_golden(_GOLDEN_PATH):
        expected = reference_pes_value(
            budget=item.budget,
            revenue=item.revenue,
            rating=item.rating,
            effective_max=item.effective_max,
        )
        assert item.expected_pes == expected, f"golden item {item.movie_id} PES not independent"


def test_evaluate_rejects_empty_sample() -> None:
    """An empty record set fails closed rather than reporting a vacuous 100% pass."""
    with pytest.raises(ValueError, match="at least one EvalRecord"):
        evaluate([], judge=DeterministicSentimentJudge())


def test_golden_fixture_is_valid_json() -> None:
    data = json.loads(_GOLDEN_PATH.read_text())
    assert isinstance(data, list) and data
    # Mutating the loaded objects must not corrupt the on-disk fixture.
    _ = copy.deepcopy(data)
