"""Extract state handler: sample movies from the source DBs (Task 6, REQ-A-6.1).

Thin I/O wrapper over the data-access layer; it duplicates no business logic. The two
read-only source SQLite files live under the directory named by the ``SQLITE_DIR`` env
var (the mounted Lambda layer at ``/opt/sqlite`` in production, the real ``_sqlite/`` in
tests). The handler opens them, resolves the ratings schema, derives the rating scale,
and samples ``n`` movies for the given ``seed`` (deterministic, REQ-A-1.3). It returns
``{"movies": [sampled_json...], "scale": scale_json}`` — the Step Functions Map input.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from handlers.shared import events
from handlers.shared.events import JsonDict
from movieintel.data_access.ratings import observe_rating_scale, resolve_ratings_schema
from movieintel.data_access.sampling import sample_movies
from movieintel.data_access.sqlite_source import open_source

#: Env var naming the directory that holds ``movies.db`` and ``ratings.db``.
SQLITE_DIR_ENV = "SQLITE_DIR"
MOVIES_DB_FILENAME = "movies.db"
RATINGS_DB_FILENAME = "ratings.db"


class SqliteDirNotConfiguredError(RuntimeError):
    """Raised when the ``SQLITE_DIR`` env var is unset; the source DBs cannot be located."""

    def __init__(self) -> None:
        super().__init__(
            f"No source DB directory configured (set {SQLITE_DIR_ENV}); "
            "the Extract handler cannot locate movies.db/ratings.db."
        )


def _sqlite_dir(env: dict[str, str] | None = None) -> Path:
    """Resolve the configured source-DB directory, raising when it is unset."""
    source = os.environ if env is None else env
    directory = source.get(SQLITE_DIR_ENV)
    if not directory:
        raise SqliteDirNotConfiguredError
    return Path(directory)


def handler(event: dict[str, Any], context: Any) -> JsonDict:
    """Sample ``event["n"]`` movies for ``event["seed"]`` and derive the rating scale.

    ``n`` defaults to 100 and ``seed`` to 0 when the event omits them, so the documented
    input-less ``start-execution`` produces the intended top-100 high-revenue eligible
    sample. Selection is deterministic (top-n by revenue desc, tie-broken by movieId),
    so ``seed`` no longer affects which movies are chosen.

    Returns ``{"movies": [sampled_json...], "scale": scale_json}``.
    """
    n = int(event.get("n", 100))
    seed = int(event.get("seed", 0))

    directory = _sqlite_dir()
    movies_conn = open_source(directory / MOVIES_DB_FILENAME)
    ratings_conn = open_source(directory / RATINGS_DB_FILENAME)
    try:
        schema = resolve_ratings_schema(ratings_conn)
        scale = observe_rating_scale(ratings_conn, schema)
        sampled = sample_movies(movies_conn, ratings_conn, n, seed)
    finally:
        movies_conn.close()
        ratings_conn.close()

    return {
        "movies": [events.sampled_to_json(movie) for movie in sampled],
        "scale": events.scale_to_json(scale),
    }
