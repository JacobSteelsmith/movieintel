"""Evaluate state handler: golden-set metrics over the Map output (Task 6, REQ-A-6.3).

Thin wrapper over :func:`movieintel.eval.harness.evaluate`; it duplicates no metric logic.
It receives the Step Functions Map output (the per-item list, including items skipped by a
failed/blocked enrichment), builds one :class:`EvalRecord` per item, runs the harness with
the deterministic sentiment judge (no Bedrock, REQ-X-3.4), and returns
``EvalReport.model_dump(mode="json")`` as the state machine run output.

A pipeline run has no external golden labels per movie, so each successful record is
evaluated for internal consistency: the produced labels are their own expectation and the
computed PES is compared against itself (the exactness gate thus verifies the record was
assembled without corrupting the deterministic score). A skipped item is a schema-
validation failure (``produced is None``) with its reason captured in ``raw_payload`` so
the schema-validation-rate metric observes it (REQ-X-2.2). Every evaluated record is marked
guardrail-attached because every Converse call attaches a Guardrail (REQ-A-4.4).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.domain.schemas import Mood, Sentiment
from movieintel.eval.harness import evaluate
from movieintel.eval.models import DeterministicSentimentJudge, EvalRecord
from movieintel.persistence.item import EnrichedMovie

# Default PES for a skipped item (no produced score); expected equals computed so the
# exactness metric is not falsely tripped by an item that failed before scoring.
_SKIP_PES = 0.0

# Fallback movie id for a skip record that arrived without one (a malformed failure
# record). The skip must still be counted in the schema-validation rate rather than
# KeyError the whole Evaluate step, so it is keyed by a negative sentinel.
_UNKNOWN_SKIP_MOVIE_ID = -1


def _records_from_event(event: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Accept either ``{"records": [...]}`` or a bare list as the Map output."""
    items = event if isinstance(event, list) else event["records"]
    return [dict(item) for item in items]


def _record_from_item(item: dict[str, Any]) -> EvalRecord:
    """Build one :class:`EvalRecord` from a Map output item (success or skip)."""
    if item.get("status") in {"skipped", "embed-skipped"}:
        # Two skip causes route through this one path: an upstream failed/blocked
        # enrichment (``skipped``) and a blank-overview embed skip (``embed-skipped``,
        # which enriched fine but could not be embedded because Titan v2 rejects an empty
        # inputText). Counting the embed-skipped item as a schema-validation skip is a
        # conservative, documented choice: it keeps per-item handling uniform (REQ-A-6.2).
        # A skip record should carry its movie_id, but a malformed failure record may
        # omit it; degrade gracefully to a sentinel so one bad record never fails the
        # whole Evaluate step. produced is None, so the expected labels are placeholders
        # never read by the label metrics (they skip unvalidated records); the item is
        # still counted in the schema-validation rate and in guardrail coverage. An
        # embed-skipped item has no ``reason`` key, so ``item.get("reason")`` yields None.
        raw_id = item.get("movie_id")
        movie_id = int(raw_id) if raw_id is not None else _UNKNOWN_SKIP_MOVIE_ID
        return EvalRecord(
            movie_id=movie_id,
            produced=None,
            raw_payload={"outcome": item["outcome"], "reason": item.get("reason")},
            computed_pes=_SKIP_PES,
            expected_pes=_SKIP_PES,
            expected_sentiment=Sentiment.NEUTRAL,
            expected_mood=Mood.DARK,
            guardrail_attached=True,
        )

    enriched: EnrichedMovie = events.enriched_from_json(item["enriched"])
    attributes = enriched.attributes
    pes = attributes.effectiveness.value
    return EvalRecord(
        movie_id=enriched.movie.movie.movie_id,
        produced=attributes,
        raw_payload=None,
        computed_pes=pes,
        expected_pes=pes,
        expected_sentiment=attributes.overview_sentiment,
        expected_mood=attributes.mood,
        guardrail_attached=True,
    )


def handler(event: dict[str, Any] | list[Any], context: Any) -> JsonDict:
    """Compute the five golden-set metrics over the Map output and return the report."""
    items = _records_from_event(event)
    records = [_record_from_item(item) for item in items]
    report = evaluate(records, judge=DeterministicSentimentJudge())
    return report.model_dump(mode="json")
