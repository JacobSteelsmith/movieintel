"""``compare_movies`` tool: deterministic side-by-side comparison (design §3.6, REQ-B-2.3).

Validates arguments against :class:`CompareMoviesArgs` (REQ-B-2.5), fetches the requested
movies via ``repository.batch_get`` (order not guaranteed — indexed by movie id), and
computes a pure, deterministic comparison across the requested dimensions
(budget/revenue/runtime/pes). Missing numeric values are reported as ``None`` and excluded
from the min/max winners (never fabricated); PES is always defined. Ties are broken by the
order the ids were requested. No AWS writes, no LLM.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.tools._ports import MovieRepository
from movieintel.agent.tools.args import CompareMoviesArgs, validate_args
from movieintel.agent.tools.results import (
    CompareMoviesMovie,
    CompareMoviesResult,
    Dimension,
    DimensionComparison,
    ToolValidationError,
)
from movieintel.persistence.item import EnrichedMovie

_ALL_DIMENSIONS: tuple[Dimension, ...] = ("budget", "revenue", "runtime", "pes")


def compare_movies(
    args: dict[str, Any], *, repository: MovieRepository
) -> CompareMoviesResult | ToolValidationError:
    """Deterministic comparison of the requested movies across the requested dimensions.

    Returns a :class:`CompareMoviesResult` on success or a :class:`ToolValidationError`
    when ``args`` fail validation (before any repository call).
    """
    validated = validate_args("compare_movies", CompareMoviesArgs, args)
    if isinstance(validated, ToolValidationError):
        return validated

    records = repository.batch_get(validated.movie_ids)
    by_id = {record.movie_id: record for record in records}

    movie_ids = validated.movie_ids
    movies = [
        CompareMoviesMovie(
            movie_id=mid,
            title=by_id[mid].movie.movie.title if mid in by_id else "",
            found=mid in by_id,
        )
        for mid in movie_ids
    ]
    missing_ids = [mid for mid in movie_ids if mid not in by_id]

    dimensions = validated.dimensions or list(_ALL_DIMENSIONS)
    comparisons = [_compare_dimension(dimension, movie_ids, by_id) for dimension in dimensions]

    return CompareMoviesResult(movies=movies, missing_ids=missing_ids, comparisons=comparisons)


def _dimension_value(record: EnrichedMovie, dimension: Dimension) -> float | None:
    """The movie's value for ``dimension``; ``None`` when the source field is missing."""
    row = record.movie.movie
    if dimension == "budget":
        return None if row.budget is None else float(row.budget)
    if dimension == "revenue":
        return None if row.revenue is None else float(row.revenue)
    if dimension == "runtime":
        return row.runtime
    return record.attributes.effectiveness.value  # pes — always defined


def _compare_dimension(
    dimension: Dimension, movie_ids: list[str], by_id: dict[str, EnrichedMovie]
) -> DimensionComparison:
    """Build the per-dimension comparison, choosing winners over defined values only.

    Iterating in ``movie_ids`` order makes tie-breaking deterministic: the first id with a
    given extreme value wins (strict ``>`` / ``<`` never replaces an equal earlier value).
    """
    values: dict[str, float | None] = {
        mid: (_dimension_value(by_id[mid], dimension) if mid in by_id else None)
        for mid in movie_ids
    }

    max_movie_id: str | None = None
    min_movie_id: str | None = None
    max_value: float | None = None
    min_value: float | None = None
    defined_count = 0

    for mid in movie_ids:
        value = values[mid]
        if value is None:
            continue
        defined_count += 1
        if max_value is None or value > max_value:
            max_value, max_movie_id = value, mid
        if min_value is None or value < min_value:
            min_value, min_movie_id = value, mid

    return DimensionComparison(
        dimension=dimension,
        values=values,
        max_movie_id=max_movie_id,
        min_movie_id=min_movie_id,
        defined_count=defined_count,
    )
