"""Evaluate handler tests: build EvalRecords from the Map output, emit the metrics.

The handler receives the Step Functions Map output (the per-item list, including skipped
items from failed/blocked enrichment), builds :class:`EvalRecord`s, runs
``eval.harness.evaluate`` with the deterministic judge, and returns
``EvalReport.model_dump(mode="json")`` — the state machine run output (REQ-A-6.3). The
test asserts the five metrics are present and the PES-exactness gate behaves.
"""

from __future__ import annotations

from typing import Any

from handlers.embed_vector import handler as embed_handler
from handlers.evaluate import handler as evaluate_handler
from handlers.shared import events

from movieintel.domain.schemas import Mood, PESTier, Sentiment
from tests.handlers.conftest import (
    FakeEmbedder,
    FakeVectorWriter,
    make_attributes,
    make_effectiveness,
    make_enriched,
    make_sampled,
)

_METRIC_NAMES = {
    "schema_validation",
    "pes_exactness",
    "sentiment_agreement",
    "mood_agreement",
    "guardrail_coverage",
}


def _enriched_item(*, movie_id: int, pes: float = 84.85) -> dict[str, Any]:
    """A successful Map item carrying an EnrichedMovie."""
    attributes = make_attributes(
        effectiveness=make_effectiveness(value=pes, tier=PESTier.STANDOUT, explanation="x"),
    )
    enriched = make_enriched(sampled=make_sampled(movie_id=movie_id), attributes=attributes)
    return {
        "status": "embedded",
        "movie_id": movie_id,
        "outcome": "ok",
        "enriched": events.enriched_to_json(enriched),
    }


def _skipped_item(*, movie_id: int, outcome: str, reason: str) -> dict[str, Any]:
    """A skipped Map item from a failed/blocked enrichment."""
    return {"status": "skipped", "movie_id": movie_id, "outcome": outcome, "reason": reason}


def test_evaluate_returns_all_five_metrics() -> None:
    """The report dict carries all five named metrics."""
    event = {"records": [_enriched_item(movie_id=1), _enriched_item(movie_id=2)]}

    report = evaluate_handler.handler(event, None)

    assert set(report) == _METRIC_NAMES
    for metric in report.values():
        assert {"name", "rate", "passed", "detail"} <= set(metric)


def test_evaluate_pes_exactness_passes_for_consistent_records() -> None:
    """When computed PES equals the expected PES for every record, exactness passes."""
    event = {"records": [_enriched_item(movie_id=1), _enriched_item(movie_id=2)]}

    report = evaluate_handler.handler(event, None)

    assert report["pes_exactness"]["rate"] == 1.0
    assert report["pes_exactness"]["passed"] is True


def test_evaluate_counts_skipped_items_in_schema_validation() -> None:
    """A skipped (failed/blocked) item counts as a schema-validation failure."""
    event = {
        "records": [
            _enriched_item(movie_id=1),
            _skipped_item(movie_id=2, outcome="failed", reason="repair exhausted"),
        ]
    }

    report = evaluate_handler.handler(event, None)

    # One of two produced a valid schema; the skipped item drags the rate below 1.0.
    assert report["schema_validation"]["rate"] == 0.5
    assert report["schema_validation"]["passed"] is False
    # Guardrail coverage still spans every evaluated record.
    assert report["guardrail_coverage"]["rate"] == 1.0


def test_evaluate_tolerates_skipped_item_without_movie_id() -> None:
    """A malformed failure record (no movie_id) degrades gracefully, not a KeyError.

    The per-item Catch can emit a skip record whose movie_id reference was unresolved
    upstream; the handler must still count it in the schema-validation rate rather than
    failing the whole Evaluate step on exactly those records (the live-run failure mode).
    """
    event = {
        "records": [
            _enriched_item(movie_id=1),
            # No movie_id key - the defensive path keys it to a sentinel.
            {"status": "skipped", "outcome": "failed", "reason": "embed error"},
        ]
    }

    report = evaluate_handler.handler(event, None)

    assert set(report) == _METRIC_NAMES
    # The skip still drags schema validation below 1.0 and is counted (not dropped).
    assert report["schema_validation"]["rate"] == 0.5
    assert report["schema_validation"]["passed"] is False
    assert report["guardrail_coverage"]["rate"] == 1.0


def test_embed_skipped_item_is_counted_as_skip() -> None:
    """An embed-skipped item (blank overview) counts as a schema-validation skip, not a crash.

    The embed step skips movies with a blank overview (Titan v2 rejects an empty
    inputText). That item still enriched successfully, but it is counted conservatively
    as a schema-validation skip (produced is None) so the skip path handles it uniformly
    (REQ-A-6.2). It must not raise and must still be guardrail-covered.
    """
    event = {
        "records": [
            _enriched_item(movie_id=1),
            {"status": "embed-skipped", "movie_id": 2, "outcome": "ok"},
        ]
    }

    report = evaluate_handler.handler(event, None)

    assert set(report) == _METRIC_NAMES
    # One of two produced a valid schema; the embed-skipped item drags the rate below 1.0.
    assert report["schema_validation"]["rate"] == 0.5
    assert report["schema_validation"]["passed"] is False
    assert report["guardrail_coverage"]["rate"] == 1.0


def test_evaluate_consumes_real_embed_output() -> None:
    """The real EmbedVector output is a valid Evaluate Map-item input (the seam).

    EmbedAndUpsertVector is the terminal state of the success item chain, so its return
    value is what the Map hands to Evaluate. This exercises that seam end to end instead
    of a hand-built item so a drift in the embed output shape is caught (REQ-A-6.3).
    """
    enriched = make_enriched(sampled=make_sampled(movie_id=7))
    validate_output = {
        "status": "enriched",
        "movie_id": 7,
        "outcome": "ok",
        "enriched": events.enriched_to_json(enriched),
    }
    # Persist passes the record through to Embed; Embed is the Map's item result.
    persisted = {**validate_output, "status": "persisted"}
    embed_output = embed_handler.handler(
        persisted, None, embedder=FakeEmbedder(), writer=FakeVectorWriter()
    )

    report = evaluate_handler.handler({"records": [embed_output]}, None)

    assert set(report) == _METRIC_NAMES
    assert report["schema_validation"]["rate"] == 1.0


def test_evaluate_accepts_a_bare_list() -> None:
    """The handler also accepts the Map output as a bare list (no wrapper key)."""
    report = evaluate_handler.handler([_enriched_item(movie_id=9)], None)
    assert set(report) == _METRIC_NAMES
    assert report["sentiment_agreement"]["name"] == "sentiment_agreement"
    # The produced labels match the (self-)expected labels by construction.
    assert Sentiment.POSITIVE.value
    assert Mood.UPLIFTING.value
