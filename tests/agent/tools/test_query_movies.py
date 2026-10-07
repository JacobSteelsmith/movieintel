"""query_movies unit tests (design §3.6; REQ-B-2.1, X-1.1, X-3.1).

Sentiment path uses the GSI (Query, PES-descending); numeric-only path uses the Scan;
``genres`` + ``limit`` + ``sort_by`` honored over the small result set. Exercised against
a moto-backed real repository, with a StubRepository to assert dispatch.
"""

from __future__ import annotations

from movieintel.agent.tools.query_movies import query_movies
from movieintel.agent.tools.results import QueryMoviesResult
from movieintel.domain.schemas import Sentiment
from movieintel.persistence.repository import MovieIntelRepository

from .conftest import StubRepository, make_enriched


def _seed(repository: MovieIntelRepository) -> None:
    """Three positive movies (varied PES) + one negative."""
    repository.put(
        make_enriched(
            movie_id=1,
            pes_value=90.0,
            sentiment=Sentiment.POSITIVE,
            title="Top",
            revenue=9_000_000,
            budget=2_000_000,
            runtime=100.0,
            genres='["Action"]',
        )
    )
    repository.put(
        make_enriched(
            movie_id=2,
            pes_value=50.0,
            sentiment=Sentiment.POSITIVE,
            title="Mid",
            revenue=1_000_000,
            budget=3_000_000,
            runtime=140.0,
            genres='["Drama"]',
        )
    )
    repository.put(
        make_enriched(
            movie_id=3,
            pes_value=70.0,
            sentiment=Sentiment.POSITIVE,
            title="High",
            revenue=5_000_000,
            budget=1_000_000,
            runtime=90.0,
            genres='["Action", "Comedy"]',
        )
    )
    repository.put(
        make_enriched(
            movie_id=4,
            pes_value=80.0,
            sentiment=Sentiment.NEGATIVE,
            title="Neg",
            revenue=4_000_000,
            budget=1_000_000,
            runtime=110.0,
            genres='["Horror"]',
        )
    )


def test_sentiment_path_uses_gsi_and_ranks_by_pes_descending(
    repository: MovieIntelRepository,
) -> None:
    _seed(repository)
    result = query_movies({"sentiment": "positive"}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    assert result.used_index is True
    assert result.sort_by == "pes"
    assert [m.movie_id for m in result.movies] == ["1", "3", "2"]
    assert result.count == 3


def test_numeric_range_only_uses_scan(repository: MovieIntelRepository) -> None:
    _seed(repository)
    result = query_movies({"min_revenue": 4_500_000}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    assert result.used_index is False
    ids = {m.movie_id for m in result.movies}
    # revenue >= 4.5M -> movie 1 (9M) and movie 3 (5M); movie 4 (4M) and 2 (1M) excluded.
    assert ids == {"1", "3"}


def test_limit_slices_the_ranked_list(repository: MovieIntelRepository) -> None:
    _seed(repository)
    result = query_movies({"sentiment": "positive", "limit": 2}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    assert [m.movie_id for m in result.movies] == ["1", "3"]
    assert result.count == 2


def test_genres_filter_is_case_insensitive_contains(repository: MovieIntelRepository) -> None:
    _seed(repository)
    result = query_movies({"sentiment": "positive", "genres": ["action"]}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    # movies 1 and 3 have Action in genres; 2 (Drama) excluded. Still PES-ranked.
    assert [m.movie_id for m in result.movies] == ["1", "3"]


def test_sentiment_and_numeric_bounds_compose(repository: MovieIntelRepository) -> None:
    _seed(repository)
    result = query_movies({"sentiment": "positive", "min_runtime": 120.0}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    # Only positive movie with runtime >= 120 is movie 2 (140). GSI order preserved.
    assert [m.movie_id for m in result.movies] == ["2"]
    assert result.used_index is True


def test_sort_by_revenue_orders_descending_with_none_last(
    repository: MovieIntelRepository,
) -> None:
    repository.put(
        make_enriched(movie_id=10, pes_value=10.0, sentiment=Sentiment.POSITIVE, revenue=3_000_000)
    )
    repository.put(
        make_enriched(movie_id=11, pes_value=20.0, sentiment=Sentiment.POSITIVE, revenue=7_000_000)
    )
    repository.put(
        make_enriched(movie_id=12, pes_value=30.0, sentiment=Sentiment.POSITIVE, revenue=None)
    )
    result = query_movies({"sentiment": "positive", "sort_by": "revenue"}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    assert result.sort_by == "revenue"
    assert [m.movie_id for m in result.movies] == ["11", "10", "12"]


def test_dispatches_to_scan_when_no_sentiment() -> None:
    stub = StubRepository(scan_result=[make_enriched(movie_id=1, pes_value=5.0)])
    result = query_movies({"min_budget": 1.0}, repository=stub)
    assert isinstance(result, QueryMoviesResult)
    assert [name for name, _ in stub.calls] == ["scan_numeric_range"]


def test_dispatches_to_query_by_sentiment_when_sentiment_given() -> None:
    stub = StubRepository(
        by_sentiment=[make_enriched(movie_id=1, pes_value=5.0, sentiment=Sentiment.NEUTRAL)]
    )
    result = query_movies({"sentiment": "neutral"}, repository=stub)
    assert isinstance(result, QueryMoviesResult)
    assert [name for name, _ in stub.calls] == ["query_by_sentiment"]


def test_result_rows_are_valid_and_count_matches(repository: MovieIntelRepository) -> None:
    _seed(repository)
    result = query_movies({"sentiment": "positive"}, repository=repository)
    assert isinstance(result, QueryMoviesResult)
    assert result.count == len(result.movies)
    row = result.movies[0]
    assert row.title == "Top"
    assert row.sentiment is Sentiment.POSITIVE
    assert row.pes == 90.0
