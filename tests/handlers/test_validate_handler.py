"""Validate handler tests: re-validate attributes, merge PES, emit an EnrichedMovie.

On an ``ok``/``repaired`` upstream outcome the handler re-validates the attributes dict
against :class:`EnrichmentAttributes`, pins the deterministic PES fields from the computed
score (the LLM cannot alter them, REQ-A-3.4), and emits an ``EnrichedMovie`` JSON. On a
``failed``/``blocked`` upstream outcome it short-circuits to a recorded skip so the Map
item is isolated and the batch continues (REQ-A-4.3, REQ-A-6.2).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.validate import handler as validate_handler

from movieintel.persistence.item import EnrichedMovie
from tests.handlers.conftest import (
    make_attributes,
    make_effectiveness,
    make_sampled,
    valid_attributes_payload,
)


def _enrich_output(
    *, outcome: str, attributes_json: dict[str, Any] | None, reason: str | None
) -> dict[str, Any]:
    """Assemble the Enrich-state output the Validate handler consumes."""
    sampled = make_sampled()
    score = make_effectiveness(value=84.85, roi_defined=True, rating_defined=True)
    return {
        "movie": events.sampled_to_json(sampled),
        "score": events.effectiveness_to_json(score),
        "result": {
            "outcome": outcome,
            "attributes": attributes_json,
            "reason": reason,
            "attempts": 0,
        },
    }


def test_validate_ok_emits_enriched_movie() -> None:
    """A valid outcome produces an EnrichedMovie whose PES equals the computed score."""
    attrs_json = events.attributes_to_json(make_attributes())
    event = _enrich_output(outcome="ok", attributes_json=attrs_json, reason=None)

    result = validate_handler.handler(event, None)

    assert result["status"] == "enriched"
    enriched = events.enriched_from_json(result["enriched"])
    assert isinstance(enriched, EnrichedMovie)
    # Deterministic PES fields come from the computed score, not the LLM echo.
    assert enriched.attributes.effectiveness.value == 84.85
    assert enriched.attributes.effectiveness.roi_defined is True
    assert enriched.attributes.effectiveness.rating_defined is True


def test_validate_preserves_pes_even_if_attributes_disagree() -> None:
    """A payload claiming a different PES value is overwritten by the computed score."""
    payload = valid_attributes_payload()
    payload["effectiveness"]["value"] = 1.23
    payload["effectiveness"]["roi_defined"] = False
    payload["effectiveness"]["rating_defined"] = False
    event = _enrich_output(outcome="repaired", attributes_json=payload, reason=None)

    result = validate_handler.handler(event, None)

    enriched = events.enriched_from_json(result["enriched"])
    assert enriched.attributes.effectiveness.value == 84.85
    assert enriched.attributes.effectiveness.roi_defined is True
    assert enriched.attributes.effectiveness.rating_defined is True


def test_validate_failed_short_circuits_to_skip() -> None:
    """A failed upstream outcome records a skip rather than fabricating a record."""
    event = _enrich_output(outcome="failed", attributes_json=None, reason="repair exhausted")

    result = validate_handler.handler(event, None)

    assert result["status"] == "skipped"
    assert result["outcome"] == "failed"
    assert result["reason"] == "repair exhausted"
    assert result["movie_id"] == make_sampled().movie.movie_id
    assert "enriched" not in result


def test_validate_blocked_short_circuits_to_skip() -> None:
    """A blocked upstream outcome records a skip too (no unsafe content persisted)."""
    event = _enrich_output(outcome="blocked", attributes_json=None, reason="guardrail intervened")

    result = validate_handler.handler(event, None)

    assert result["status"] == "skipped"
    assert result["outcome"] == "blocked"
    assert result["reason"] == "guardrail intervened"
