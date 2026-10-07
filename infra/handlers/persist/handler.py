"""Persist state handler: idempotent upsert into DynamoDB (Task 6, REQ-A-6.1).

Thin wrapper over :class:`movieintel.persistence.repository.MovieIntelRepository`; it
duplicates no persistence logic. :class:`PersistenceConfig` names the table/GSI (no inline
literals). The DynamoDB ``Table`` resource is injectable for tests (moto) and defaults to a
boto3 resource built from ``config.region`` for the live path. The put is idempotent
(last-write-wins), so re-runs replace rather than duplicate (REQ-A-5.5).
"""

from __future__ import annotations

from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.repository import MovieIntelRepository


def _default_table(config: PersistenceConfig) -> Any:
    """Build the live DynamoDB ``Table`` resource from the configured region.

    Imported lazily so the module imports cleanly without AWS creds (tests inject a
    moto table and never reach this path).
    """
    import boto3

    resource = boto3.resource("dynamodb", region_name=config.region)
    return resource.Table(config.table_name)


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    table: Any | None = None,
    config: PersistenceConfig | None = None,
) -> JsonDict:
    """Upsert ``event["enriched"]`` into the ``MovieIntel`` table."""
    config = config or PersistenceConfig.from_env()
    table = table if table is not None else _default_table(config)

    enriched = events.enriched_from_json(event["enriched"])
    repository = MovieIntelRepository(table=table, config=config)
    repository.put(enriched)

    # Pass the enriched payload (and upstream outcome) through so the chained
    # EmbedAndUpsertVector state (which replaces its input with this output under
    # payload_response_only) still receives the record it needs under ``event["enriched"]``
    # and can forward ``outcome`` to the Evaluate state.
    return {
        "status": "persisted",
        "movie_id": enriched.movie.movie.movie_id,
        "outcome": event.get("outcome", "ok"),
        "enriched": event["enriched"],
    }
