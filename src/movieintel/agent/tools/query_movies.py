"""``query_movies`` tool: structured DynamoDB filter/compare over movies (design §3.6).

Dispatches to the right repository access pattern (amazon-dynamodb skill, design §3.4):

- When a ``sentiment`` is given there IS a partition to query — ``GSI1PK =
  SENTIMENT#<sentiment>`` with ``GSI1SK`` the zero-padded PES — so
  ``repository.query_by_sentiment`` runs ``Query GSI1`` with ``ScanIndexForward=False`` and
  returns that sentiment's movies ranked by PES descending. (The skill is explicit that a
  ``FilterExpression`` is applied AFTER items are read/billed and never substitutes for a
  key, so when a partition exists the Query path is the cost-appropriate one.)
- When the request is sentiment-agnostic there is no partition key to query, so the honest
  path is ``repository.scan_numeric_range`` — a filtered ``Scan``, acceptable only because
  the dataset is 50-100 items (design §7.3 documented small-N tradeoff).

``genres``, non-PES ``sort_by``, numeric bounds supplied alongside a sentiment, and
``limit`` are then applied in Python over the small result set (the GSI has no numeric
filter; filtering in-process preserves the PES ranking from the index).

Arguments are validated against :class:`QueryMoviesArgs` BEFORE any repository call; invalid
input returns a :class:`ToolValidationError` and never executes (REQ-B-2.5).
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.tools._ports import MovieRepository
from movieintel.agent.tools.args import QueryMoviesArgs, validate_args
from movieintel.agent.tools.results import (
    QueryMoviesMovie,
    QueryMoviesResult,
    SortBy,
    ToolValidationError,
)
from movieintel.persistence.item import EnrichedMovie

_DEFAULT_SORT: SortBy = "pes"


def query_movies(
    args: dict[str, Any], *, repository: MovieRepository
) -> QueryMoviesResult | ToolValidationError:
    """Filter enriched movies by sentiment + numeric ranges, optionally ranked by PES.

    Returns a :class:`QueryMoviesResult` on success or a :class:`ToolValidationError` when
    ``args`` fail validation (before any repository call).
    """
    validated = validate_args("query_movies", QueryMoviesArgs, args)
    if isinstance(validated, ToolValidationError):
        return validated

    if validated.sentiment is not None:
        records = repository.query_by_sentiment(validated.sentiment)
        used_index = True
    else:
        records = repository.scan_numeric_range(
            min_budget=validated.min_budget,
            max_budget=validated.max_budget,
            min_revenue=validated.min_revenue,
            max_revenue=validated.max_revenue,
            min_runtime=validated.min_runtime,
            max_runtime=validated.max_runtime,
        )
        used_index = False

    # When a sentiment was queried, apply the numeric bounds in Python (the GSI has no
    # numeric filter); over 50-100 items this is the documented small-N approach.
    if used_index:
        records = [r for r in records if _within_numeric_bounds(r, validated)]

    if validated.genres:
        records = [r for r in records if _matches_genres(r, validated.genres)]

    sort_by: SortBy = validated.sort_by or _DEFAULT_SORT
    records = _sort_records(records, sort_by, used_index)

    if validated.limit is not None:
        records = records[: validated.limit]

    movies = [_to_row(r) for r in records]
    return QueryMoviesResult(
        count=len(movies), sort_by=sort_by, used_index=used_index, movies=movies
    )


def _within_numeric_bounds(record: EnrichedMovie, args: QueryMoviesArgs) -> bool:
    """True when the record satisfies every supplied budget/revenue/runtime bound.

    A missing value on the record fails any bound that constrains that attribute (a movie
    with no budget cannot satisfy a ``min_budget``), matching the Scan path, where DynamoDB
    filters out items lacking the attribute.
    """
    row = record.movie.movie
    checks: list[tuple[float | None, float | None, float | None]] = [
        (row.budget, args.min_budget, args.max_budget),
        (row.revenue, args.min_revenue, args.max_revenue),
        (row.runtime, args.min_runtime, args.max_runtime),
    ]
    for value, lo, hi in checks:
        if lo is not None or hi is not None:
            if value is None:
                return False
            if lo is not None and value < lo:
                return False
            if hi is not None and value > hi:
                return False
    return True


def _matches_genres(record: EnrichedMovie, genres: list[str]) -> bool:
    """Case-insensitive containment: any requested genre appears in the raw genres text."""
    raw = record.movie.movie.genres
    if raw is None:
        return False
    haystack = raw.lower()
    return any(genre.lower() in haystack for genre in genres)


def _sort_key(record: EnrichedMovie, sort_by: SortBy) -> float | None:
    row = record.movie.movie
    if sort_by == "pes":
        return record.attributes.effectiveness.value
    if sort_by == "revenue":
        return None if row.revenue is None else float(row.revenue)
    if sort_by == "budget":
        return None if row.budget is None else float(row.budget)
    return row.runtime  # runtime (float | None)


def _sort_records(
    records: list[EnrichedMovie], sort_by: SortBy, used_index: bool
) -> list[EnrichedMovie]:
    """Sort descending with ``None`` values last; PES on the GSI is already ordered.

    When ``sort_by == "pes"`` and the GSI (sentiment) path was used, the records already
    arrive PES-descending, so re-sorting is unnecessary and the GSI order is preserved.
    """
    if sort_by == "pes" and used_index:
        return records
    # None sorts last regardless of direction: (is_missing, -value).
    return sorted(
        records,
        key=lambda r: (_sort_key(r, sort_by) is None, -(_sort_key(r, sort_by) or 0.0)),
    )


def _to_row(record: EnrichedMovie) -> QueryMoviesMovie:
    row = record.movie.movie
    eff = record.attributes.effectiveness
    return QueryMoviesMovie(
        movie_id=record.movie_id,
        title=row.title,
        genres=row.genres,
        sentiment=record.attributes.overview_sentiment,
        budget=row.budget,
        revenue=row.revenue,
        runtime=row.runtime,
        pes=eff.value,
        pes_tier=eff.tier,
        mood=record.attributes.mood,
    )
