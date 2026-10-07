"""Enrich state handler: Bedrock Converse enrichment for one movie (Task 6, REQ-A-6.1).

Thin wrapper over :func:`movieintel.enrichment.enrich_movie`; it duplicates no enrichment,
repair, or Guardrail logic. :class:`BedrockConfig` is built from the environment (so the
Guardrail id wired in by CDK is honored on every call). The ``bedrock-runtime`` client is
injectable for tests and defaults to a boto3 client built from ``config.region`` for the
live path. The ``Ok|Repaired|Failed|Blocked`` outcome is serialized into the shared
envelope; the movie and score pass through so the Validate state can assemble the record.
"""

from __future__ import annotations

import logging
from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.config import BedrockConfig
from movieintel.enrichment import enrich_movie
from movieintel.enrichment.client import BedrockConverseClient

logger = logging.getLogger(__name__)

# Max characters of a failure/block reason to log; longer reasons are truncated with "…".
_MAX_REASON_LEN = 200


def _log_outcome(movie_id: int, envelope: JsonDict) -> None:
    """Emit one structured INFO line per movie for CloudWatch Logs Insights.

    Records only ``movie_id``, the outcome kind, the repair ``attempts``, and a short,
    truncated reason (``failed``/``blocked`` only). Never logs movie content or any other
    potentially sensitive field. This is a thin observability addition that reads the
    already-serialized envelope and does not touch the returned payload.
    """
    reason = envelope.get("reason")
    if isinstance(reason, str) and reason:
        short_reason = reason if len(reason) <= _MAX_REASON_LEN else reason[:_MAX_REASON_LEN] + "…"
    else:
        short_reason = "-"
    logger.info(
        "enrichment outcome movie_id=%s outcome=%s attempts=%s reason=%s",
        movie_id,
        envelope["outcome"],
        envelope["attempts"],
        short_reason,
    )


def _default_client(config: BedrockConfig) -> BedrockConverseClient:
    """Build the live ``bedrock-runtime`` client from the configured region.

    Imported lazily so the module imports cleanly in environments without AWS creds
    (tests always inject a client and never reach this path).
    """
    import boto3

    client: BedrockConverseClient = boto3.client("bedrock-runtime", region_name=config.region)
    return client


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    client: BedrockConverseClient | None = None,
    config: BedrockConfig | None = None,
) -> JsonDict:
    """Enrich ``event["movie"]`` with its computed ``event["score"]`` via Bedrock.

    Returns ``{"movie": sampled, "score": effectiveness, "result": envelope}`` where the
    envelope is ``{"outcome", "attributes"|null, "reason"|null, "attempts"}``.
    """
    config = config or BedrockConfig.from_env()
    client = client or _default_client(config)

    sampled = events.sampled_from_json(event["movie"])
    score = events.effectiveness_from_json(event["score"])

    result = enrich_movie(sampled, score, client=client, config=config)

    result_envelope = events.result_to_json(result)
    _log_outcome(sampled.movie.movie_id, result_envelope)

    return {
        "movie": event["movie"],
        "score": event["score"],
        "result": result_envelope,
    }
