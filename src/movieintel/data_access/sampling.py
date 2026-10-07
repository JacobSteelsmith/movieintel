"""Deterministic high-revenue movie sampling, joined to aggregated ratings (REQ-A-1.3)."""

from __future__ import annotations

import sqlite3

from movieintel.data_access.models import SampledMovie
from movieintel.data_access.movies import (
    MOVIES_TABLE,
    _quote_identifier,
    _resolve_movies_columns,
    read_movies_by_ids,
)
from movieintel.data_access.ratings import RATINGS_TABLE, resolve_ratings_schema

MIN_SAMPLE = 50
MAX_SAMPLE = 100


def sample_movies(
    conn_movies: sqlite3.Connection,
    conn_ratings: sqlite3.Connection,
    n: int,
    seed: int,
) -> list[SampledMovie]:
    """Return a deterministic, high-revenue-biased sample of ``n`` movies joined to ratings.

    Selection rule: the top ``n`` ELIGIBLE movies by revenue descending, tie-broken by
    movieId ascending. ``eligible`` = a non-blank overview AND at least one joinable
    ratings row (REQ-A-1.3). This biases the sample toward high-revenue
    Action/blockbuster titles, which also tend to have more ratings, so canned demo
    queries have real candidates.

    The rule is fully deterministic and INDEPENDENT of ``seed`` (every seed yields the
    identical top-``n`` set); the ``seed`` parameter is retained only for signature/caller
    backward compatibility. The count is bounded to ``50 <= n <= 100`` (REQ-A-1.3). Each
    selected movie is left-joined to ``AVG(rating)``/``COUNT(*)`` on the resolved movie
    key; since eligibility requires ratings, every result has ``rating`` set and
    ``rating_count >= 1``.
    """
    if not MIN_SAMPLE <= n <= MAX_SAMPLE:
        raise ValueError(f"n must be between {MIN_SAMPLE} and {MAX_SAMPLE} inclusive, got {n}")

    chosen_ids = _choose_movie_ids(conn_movies, conn_ratings, n)
    movies = read_movies_by_ids(conn_movies, chosen_ids)
    ratings_by_id = _aggregate_ratings(conn_ratings, chosen_ids)

    sampled: list[SampledMovie] = []
    for movie in movies:
        avg_rating, count = ratings_by_id.get(movie.movie_id, (None, 0))
        sampled.append(SampledMovie(movie=movie, rating=avg_rating, rating_count=count))
    return sampled


def _choose_movie_ids(
    conn_movies: sqlite3.Connection,
    conn_ratings: sqlite3.Connection,
    n: int,
) -> list[int]:
    """Pick the top ``n`` eligible movie ids by revenue desc, tie-broken by movieId asc.

    Eligible = non-blank overview (enforced in SQL) AND present in the distinct set of
    rated movie ids (joinable ratings). Deterministic and seed-independent. The returned
    ids are sorted ascending to preserve ``read_movies_by_ids``' ordering contract.
    """
    rated_ids = _rated_movie_ids(conn_ratings)

    resolved = _resolve_movies_columns(conn_movies)
    key_col = _quote_identifier(resolved["movie_id"])
    revenue_col = _quote_identifier(resolved["revenue"])
    overview_col = _quote_identifier(resolved["overview"])
    sql = (
        f"SELECT {key_col}, {revenue_col} FROM {_quote_identifier(MOVIES_TABLE)} "
        f"WHERE {overview_col} IS NOT NULL AND TRIM({overview_col}) <> ''"
    )

    eligible: list[tuple[int, int]] = []
    for raw_id, raw_revenue in conn_movies.execute(sql):
        movie_id = int(raw_id)
        if movie_id not in rated_ids:
            continue
        eligible.append((movie_id, _coerce_revenue(raw_revenue)))

    eligible.sort(key=lambda item: (-item[1], item[0]))
    return sorted(movie_id for movie_id, _ in eligible[:n])


def _rated_movie_ids(conn_ratings: sqlite3.Connection) -> set[int]:
    """Return the set of distinct movie ids that have at least one ratings row."""
    schema = resolve_ratings_schema(conn_ratings)
    key_col = _quote_identifier(schema.movie_key_column)
    table = _quote_identifier(RATINGS_TABLE)
    return {int(row[0]) for row in conn_ratings.execute(f"SELECT DISTINCT {key_col} FROM {table}")}


def _coerce_revenue(raw_revenue: object) -> int:
    """Coerce a stored revenue to int, treating NULL/blank/non-numeric as 0."""
    if raw_revenue in (None, ""):
        return 0
    if isinstance(raw_revenue, int):
        return raw_revenue
    if isinstance(raw_revenue, float):
        return int(raw_revenue)
    try:
        return int(float(str(raw_revenue)))
    except (TypeError, ValueError):
        return 0


def _aggregate_ratings(
    conn_ratings: sqlite3.Connection,
    movie_ids: list[int],
) -> dict[int, tuple[float, int]]:
    """Return ``{movie_id: (avg_rating, count)}`` for the given ids via one grouped query."""
    if not movie_ids:
        return {}
    schema = resolve_ratings_schema(conn_ratings)
    key_col = _quote_identifier(schema.movie_key_column)
    rating_col = _quote_identifier(schema.rating_column)
    table = _quote_identifier(RATINGS_TABLE)
    placeholders = ", ".join("?" for _ in movie_ids)
    sql = (
        f"SELECT {key_col}, AVG({rating_col}), COUNT(*) "
        f"FROM {table} WHERE {key_col} IN ({placeholders}) GROUP BY {key_col}"
    )
    result: dict[int, tuple[float, int]] = {}
    for key, avg_rating, count in conn_ratings.execute(sql, tuple(movie_ids)):
        result[int(key)] = (float(avg_rating), int(count))
    return result
