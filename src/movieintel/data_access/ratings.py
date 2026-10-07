"""Resolve the undocumented ``ratings`` schema and derive its scale (REQ-A-1.2, 1.5, 1.6)."""

from __future__ import annotations

import sqlite3

from movieintel.data_access.errors import MissingRatingsColumnError
from movieintel.data_access.models import RatingScale, RatingsSchema
from movieintel.data_access.sqlite_source import introspect_table

#: Ranked candidate names for the movie-key role, matched case-insensitively.
RATINGS_MOVIE_KEY_CANDIDATES: tuple[str, ...] = (
    "movieId",
    "movie_id",
    "movieid",
    "mid",
    "film_id",
)

#: Ranked candidate names for the numeric rating role, matched case-insensitively.
RATINGS_RATING_CANDIDATES: tuple[str, ...] = (
    "rating",
    "score",
    "stars",
    "value",
    "avg_rating",
)

RATINGS_TABLE = "ratings"


def _resolve_role(
    role: str,
    candidates: tuple[str, ...],
    available: list[str],
) -> str:
    """Pick the highest-ranked candidate present in ``available`` (case-insensitive).

    Returns the original-cased column name from ``available``. Raises
    :class:`MissingRatingsColumnError` when no candidate matches (REQ-A-1.5).
    """
    lowered = {name.lower(): name for name in available}
    for candidate in candidates:
        match = lowered.get(candidate.lower())
        if match is not None:
            return match
    raise MissingRatingsColumnError(role, available)


def resolve_ratings_schema(conn: sqlite3.Connection) -> RatingsSchema:
    """Introspect ``ratings`` and resolve the movie-key and rating roles.

    Resolution is driven by the introspected columns plus ranked candidate names
    (never a single hard-coded name), so it tolerates schema casing differences
    and surfaces a clear error on an unresolved role (REQ-A-1.2, 1.5).
    """
    columns = [col.name for col in introspect_table(conn, RATINGS_TABLE)]
    if not columns:
        raise MissingRatingsColumnError("movie_key", columns)
    movie_key = _resolve_role("movie_key", RATINGS_MOVIE_KEY_CANDIDATES, columns)
    rating = _resolve_role("rating", RATINGS_RATING_CANDIDATES, columns)
    return RatingsSchema(
        movie_key_column=movie_key,
        rating_column=rating,
        all_columns=columns,
    )


def observe_rating_scale(
    conn: sqlite3.Connection,
    schema: RatingsSchema,
    *,
    declared_max: float = 5.0,
    fallback_default: float = 5.0,
) -> RatingScale:
    """Derive the rating scale/range from the ratings data (REQ-A-1.6).

    ``effective_max`` is the observed maximum when the table has rows; otherwise
    the ``fallback_default`` is used and ``fallback_used`` is set True so the
    assumption is surfaced explicitly rather than applied silently (REQ-A-3.1).
    """
    rating_col = _quote_identifier(schema.rating_column)
    table = _quote_identifier(RATINGS_TABLE)
    row = conn.execute(f"SELECT MIN({rating_col}), MAX({rating_col}) FROM {table}").fetchone()
    observed_min = None if row is None or row[0] is None else float(row[0])
    observed_max = None if row is None or row[1] is None else float(row[1])

    if observed_max is not None:
        return RatingScale(
            observed_min=observed_min,
            observed_max=observed_max,
            declared_max=declared_max,
            effective_max=observed_max,
            fallback_used=False,
            fallback_default=fallback_default,
        )
    return RatingScale(
        observed_min=observed_min,
        observed_max=observed_max,
        declared_max=declared_max,
        effective_max=fallback_default,
        fallback_used=True,
        fallback_default=fallback_default,
    )


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQL identifier (resolved from introspection, not user input)."""
    return '"' + identifier.replace('"', '""') + '"'
