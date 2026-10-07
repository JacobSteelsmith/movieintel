"""ComputePES state handler: deterministic PES for one movie (Task 6, REQ-A-6.1).

Thin wrapper over :func:`movieintel.domain.pes.score_sampled_movie`; it duplicates no
scoring logic. Receives ``{"movie": sampled, "scale": scale}`` and returns
``{"movie": sampled, "score": effectiveness}`` so the next state (Enrich) gets the movie
plus the deterministic score the LLM must preserve byte-for-byte (REQ-A-3.4).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.domain.pes import score_sampled_movie


def handler(event: dict[str, Any], context: Any) -> JsonDict:
    """Compute the deterministic PES for ``event["movie"]`` using ``event["scale"]``."""
    sampled = events.sampled_from_json(event["movie"])
    scale = events.scale_from_json(event["scale"])
    score = score_sampled_movie(sampled, scale)
    return {
        "movie": events.sampled_to_json(sampled),
        "score": events.effectiveness_to_json(score),
    }
