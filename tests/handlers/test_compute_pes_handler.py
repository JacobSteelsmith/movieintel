"""ComputePES handler tests: thin wrapper over ``domain.pes.score_sampled_movie``.

The handler receives ``{"movie": sampled, "scale": scale}`` and returns
``{"movie": sampled, "score": effectiveness}`` where the score is the deterministic PES.
It must reuse the domain function and alter nothing about the movie it passes through.
"""

from __future__ import annotations

from handlers.compute_pes import handler as compute_pes_handler
from handlers.shared import events

from movieintel.domain.pes import score_sampled_movie
from tests.handlers.conftest import compute_pes_event, make_sampled, make_scale


def test_compute_pes_returns_deterministic_score() -> None:
    """The handler's score equals the domain function computed independently."""
    sampled = make_sampled(budget=1_000_000, revenue=5_000_000, rating=4.0)
    scale = make_scale(effective_max=5.0)
    event = compute_pes_event(sampled=sampled, scale=scale)

    result = compute_pes_handler.handler(event, None)

    expected = score_sampled_movie(sampled, scale)
    assert events.effectiveness_from_json(result["score"]) == expected


def test_compute_pes_passes_the_movie_through_unchanged() -> None:
    """The movie in the output is byte-identical to the input movie."""
    sampled = make_sampled()
    event = compute_pes_event(sampled=sampled)

    result = compute_pes_handler.handler(event, None)

    assert result["movie"] == event["movie"]
    assert events.sampled_from_json(result["movie"]) == sampled


def test_compute_pes_handles_missing_budget_without_error() -> None:
    """A movie with no budget still yields a defined PES (roi_defined False)."""
    sampled = make_sampled(budget=None, rating=4.0)
    event = compute_pes_event(sampled=sampled, scale=make_scale())

    result = compute_pes_handler.handler(event, None)

    score = events.effectiveness_from_json(result["score"])
    assert score.roi_defined is False
    assert score.rating_defined is True
