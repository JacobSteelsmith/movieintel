"""compare_movies unit + math tests (design §3.6; REQ-B-2.3, X-1.1, X-3.1).

Deterministic comparative math across budget/revenue/runtime/pes, honouring missing
values honestly (``None`` excluded from min/max, never fabricated), with ties broken by
``movie_ids`` order and ``missing_ids`` for batch_get misses.
"""

from __future__ import annotations

from movieintel.agent.tools.compare_movies import compare_movies
from movieintel.agent.tools.results import CompareMoviesResult, DimensionComparison
from movieintel.domain.schemas import PESTier, Sentiment

from .conftest import StubRepository, make_enriched


def _by_dimension(result: CompareMoviesResult) -> dict[str, DimensionComparison]:
    return {c.dimension: c for c in result.comparisons}


def test_two_movies_all_dimensions_values_and_winners() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(
                movie_id=1,
                pes_value=90.0,
                title="A",
                budget=2_000_000,
                revenue=9_000_000,
                runtime=100.0,
            ),
            make_enriched(
                movie_id=2,
                pes_value=50.0,
                title="B",
                budget=3_000_000,
                revenue=1_000_000,
                runtime=140.0,
            ),
        ]
    )
    result = compare_movies({"movie_ids": ["1", "2"]}, repository=repo)
    assert isinstance(result, CompareMoviesResult)
    assert result.missing_ids == []
    comps = _by_dimension(result)
    assert set(comps) == {"budget", "revenue", "runtime", "pes"}

    budget = result.comparisons[0]
    assert comps["budget"].values == {"1": 2_000_000.0, "2": 3_000_000.0}
    assert comps["budget"].max_movie_id == "2"
    assert comps["budget"].min_movie_id == "1"
    assert comps["revenue"].max_movie_id == "1"
    assert comps["runtime"].max_movie_id == "2"
    assert comps["pes"].max_movie_id == "1"
    assert budget.defined_count == 2


def test_missing_numeric_value_is_none_and_excluded_from_min_max() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(movie_id=1, pes_value=10.0, budget=None, revenue=5_000_000),
            make_enriched(movie_id=2, pes_value=20.0, budget=4_000_000, revenue=6_000_000),
        ]
    )
    result = compare_movies({"movie_ids": ["1", "2"], "dimensions": ["budget"]}, repository=repo)
    assert isinstance(result, CompareMoviesResult)
    (budget,) = result.comparisons
    assert budget.values == {"1": None, "2": 4_000_000.0}
    assert budget.max_movie_id == "2"
    assert budget.min_movie_id == "2"
    assert budget.defined_count == 1


def test_unknown_tier_and_roi_undefined_still_compare_on_pes() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(
                movie_id=1,
                pes_value=42.5,
                budget=None,
                budget_tier="unknown",
                revenue_tier="unknown",
                pes_tier=None,
            ),
            make_enriched(
                movie_id=2, pes_value=84.0, budget=0, budget_tier="unknown", pes_tier=None
            ),
        ]
    )
    result = compare_movies({"movie_ids": ["1", "2"], "dimensions": ["pes"]}, repository=repo)
    assert isinstance(result, CompareMoviesResult)
    (pes,) = result.comparisons
    assert pes.values == {"1": 42.5, "2": 84.0}
    assert pes.max_movie_id == "2"
    assert pes.min_movie_id == "1"
    assert pes.defined_count == 2


def test_dimensions_subset_honored() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(movie_id=1, pes_value=10.0),
            make_enriched(movie_id=2, pes_value=20.0),
        ]
    )
    result = compare_movies(
        {"movie_ids": ["1", "2"], "dimensions": ["revenue", "pes"]}, repository=repo
    )
    assert isinstance(result, CompareMoviesResult)
    assert [c.dimension for c in result.comparisons] == ["revenue", "pes"]


def test_missing_ids_populated_when_batch_get_omits_one() -> None:
    repo = StubRepository(batch=[make_enriched(movie_id=1, pes_value=10.0, title="Found")])
    result = compare_movies({"movie_ids": ["1", "2"]}, repository=repo)
    assert isinstance(result, CompareMoviesResult)
    assert result.missing_ids == ["2"]
    found = {m.movie_id: m for m in result.movies}
    assert found["1"].found is True
    assert found["2"].found is False
    assert found["2"].title == ""
    # Missing movie contributes None in every dimension.
    comps = _by_dimension(result)
    pes = comps["pes"]
    assert pes.values["2"] is None
    assert pes.values["1"] == 10.0


def test_ties_broken_by_movie_ids_order() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(movie_id=2, pes_value=50.0),
            make_enriched(movie_id=1, pes_value=50.0),
        ]
    )
    result = compare_movies({"movie_ids": ["1", "2"], "dimensions": ["pes"]}, repository=repo)
    assert isinstance(result, CompareMoviesResult)
    (pes,) = result.comparisons
    # Both PES equal; winner is the first in the requested order.
    assert pes.max_movie_id == "1"
    assert pes.min_movie_id == "1"


def test_batch_get_called_once_with_requested_ids() -> None:
    repo = StubRepository(
        batch=[
            make_enriched(
                movie_id=1, pes_value=10.0, sentiment=Sentiment.POSITIVE, pes_tier=PESTier.SOLID
            ),
            make_enriched(movie_id=2, pes_value=20.0),
        ]
    )
    compare_movies({"movie_ids": ["1", "2"]}, repository=repo)
    assert [name for name, _ in repo.calls] == ["batch_get"]
    assert repo.calls[0][1]["movie_ids"] == ["1", "2"]
