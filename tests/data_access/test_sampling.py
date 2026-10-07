"""Tests for read_movies and reproducible sample_movies (REQ-A-1.1, 1.3)."""

from __future__ import annotations

import sqlite3

import pytest

from movieintel.data_access.models import MovieRow
from movieintel.data_access.movies import read_movies
from movieintel.data_access.sampling import sample_movies


def test_read_movies_returns_typed_rows(movies_conn: sqlite3.Connection) -> None:
    rows = read_movies(movies_conn, limit=5)
    assert len(rows) == 5
    assert all(isinstance(r, MovieRow) for r in rows)
    assert all(r.movie_id > 0 for r in rows)
    assert all(r.title for r in rows)
    # Ordered by movie key.
    assert [r.movie_id for r in rows] == sorted(r.movie_id for r in rows)


def test_sample_is_reproducible_for_same_seed(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    first = sample_movies(movies_conn, ratings_conn, n=60, seed=42)
    second = sample_movies(movies_conn, ratings_conn, n=60, seed=42)
    assert [s.movie.movie_id for s in first] == [s.movie.movie_id for s in second]
    assert [s.rating for s in first] == [s.rating for s in second]
    assert [s.rating_count for s in first] == [s.rating_count for s in second]


def test_seed_is_irrelevant_to_selection(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    # Selection is now deterministic by revenue (top-N-by-revenue rule), so the seed no
    # longer affects which movies are chosen: different seeds yield the identical set.
    # (Replaces the old uniform-random seed-sensitivity assertion, which no longer holds.)
    a = sample_movies(movies_conn, ratings_conn, n=60, seed=42)
    b = sample_movies(movies_conn, ratings_conn, n=60, seed=7)
    assert [s.movie.movie_id for s in a] == [s.movie.movie_id for s in b]


@pytest.mark.parametrize("n", [49, 101, 0, 1000])
def test_bounds_rejected(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection, n: int
) -> None:
    with pytest.raises(ValueError):
        sample_movies(movies_conn, ratings_conn, n=n, seed=1)


@pytest.mark.parametrize("n", [50, 100])
def test_bounds_accepted(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection, n: int
) -> None:
    sample = sample_movies(movies_conn, ratings_conn, n=n, seed=1)
    assert len(sample) == n


def test_every_sampled_movie_is_rated(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    # The eligibility filter now excludes unrated movies by construction, so every
    # selected movie has a joinable ratings aggregate. (Replaces the old assertion that
    # most sampled movies had NO ratings, which the eligibility filter now contradicts.)
    sample = sample_movies(movies_conn, ratings_conn, n=100, seed=123)
    for s in sample:
        assert s.rating is not None
        assert s.rating_count >= 1
        assert 0.5 <= s.rating <= 5.0


def _eligible_pool_by_revenue(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> list[tuple[int, int]]:
    """Build the eligible pool (non-blank overview AND rated) ordered by (-revenue, id).

    Returns ``[(movie_id, revenue), ...]`` using the resolved schema column names, so the
    test mirrors the production selection rule without depending on its implementation.
    """
    from movieintel.data_access.movies import (
        MOVIES_TABLE,
        _quote_identifier,
        _resolve_movies_columns,
    )
    from movieintel.data_access.ratings import RATINGS_TABLE, resolve_ratings_schema

    rating_schema = resolve_ratings_schema(ratings_conn)
    rated_key = _quote_identifier(rating_schema.movie_key_column)
    rated_ids = {
        int(row[0])
        for row in ratings_conn.execute(
            f"SELECT DISTINCT {rated_key} FROM {_quote_identifier(RATINGS_TABLE)}"
        )
    }

    resolved = _resolve_movies_columns(movies_conn)
    key_col = _quote_identifier(resolved["movie_id"])
    revenue_col = _quote_identifier(resolved["revenue"])
    overview_col = _quote_identifier(resolved["overview"])
    sql = (
        f"SELECT {key_col}, {revenue_col} FROM {_quote_identifier(MOVIES_TABLE)} "
        f"WHERE {overview_col} IS NOT NULL AND TRIM({overview_col}) <> ''"
    )
    pool: list[tuple[int, int]] = []
    for movie_id, revenue in movies_conn.execute(sql):
        mid = int(movie_id)
        if mid not in rated_ids:
            continue
        try:
            rev = int(revenue) if revenue not in (None, "") else 0
        except (TypeError, ValueError):
            rev = 0
        pool.append((mid, rev))
    pool.sort(key=lambda item: (-item[1], item[0]))
    return pool


def test_sample_is_top_n_by_revenue_over_eligible_pool(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    pool = _eligible_pool_by_revenue(movies_conn, ratings_conn)
    expected_top = sorted(mid for mid, _ in pool[:100])

    sample = sample_movies(movies_conn, ratings_conn, n=100, seed=0)
    assert [s.movie.movie_id for s in sample] == expected_top


def test_sample_min_revenue_exceeds_unselected_eligible_max(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    pool = _eligible_pool_by_revenue(movies_conn, ratings_conn)
    selected_ids = {mid for mid, _ in pool[:100]}
    selected_revenues = [rev for mid, rev in pool if mid in selected_ids]
    unselected_revenues = [rev for mid, rev in pool if mid not in selected_ids]

    sample = sample_movies(movies_conn, ratings_conn, n=100, seed=0)
    assert {s.movie.movie_id for s in sample} == selected_ids
    assert min(selected_revenues) >= max(unselected_revenues)


def test_sample_excludes_blank_overview_and_unrated(
    movies_conn: sqlite3.Connection, ratings_conn: sqlite3.Connection
) -> None:
    sample = sample_movies(movies_conn, ratings_conn, n=100, seed=0)
    for s in sample:
        assert s.movie.overview is not None
        assert s.movie.overview.strip() != ""
        assert s.rating is not None
        assert s.rating_count >= 1


def test_demo_main_runs(capsys: pytest.CaptureFixture[str]) -> None:
    from movieintel.data_access.demo import main

    exit_code = main(["--n", "50", "--seed", "42"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "movies schema" in out
    assert "ratings schema" in out
    assert "derived rating scale" in out
    assert "sample (n=50" in out
