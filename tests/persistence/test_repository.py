"""Repository tests against moto DynamoDB (REQ-A-5.1/.4/.5)."""

from __future__ import annotations

from typing import Any

from movieintel.domain.schemas import Mood, PESTier, Sentiment, Tier
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.repository import MovieIntelRepository


def _repo(table: Any, config: PersistenceConfig) -> MovieIntelRepository:
    return MovieIntelRepository(table=table, config=config)


def test_round_trip_preserves_all_five_attributes_and_pes_components(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    """put -> get reconstructs the exact record: all 5 attributes + PES survive (REQ-A-5.4)."""
    record = enriched_record(
        movie_id=7,
        pes_value=84.85,
        sentiment=Sentiment.POSITIVE,
        mood=Mood.INTENSE,
        budget_tier=Tier.MEDIUM,
        revenue_tier=Tier.HIGH,
        pes_tier=PESTier.STANDOUT,
    )
    repo = _repo(dynamodb_table, config)
    repo.put(record)

    fetched = repo.get(7)
    assert fetched is not None
    # Whole-record equality proves lossless round-trip (float restored from Decimal).
    assert fetched == record
    attrs = fetched.attributes
    assert attrs.overview_sentiment is Sentiment.POSITIVE
    assert attrs.budget_tier is Tier.MEDIUM
    assert attrs.revenue_tier is Tier.HIGH
    assert attrs.mood is Mood.INTENSE
    eff = attrs.effectiveness
    assert eff.value == 84.85
    assert eff.tier is PESTier.STANDOUT
    assert eff.explanation == "Because reasons."
    assert eff.roi_defined is True
    assert eff.rating_defined is True


def test_get_missing_returns_none(dynamodb_table: Any, config: PersistenceConfig) -> None:
    assert _repo(dynamodb_table, config).get(999) is None


def test_unknown_tiers_round_trip(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    record = enriched_record(
        movie_id=11, pes_value=10.0, budget_tier="unknown", revenue_tier="unknown"
    )
    repo = _repo(dynamodb_table, config)
    repo.put(record)
    fetched = repo.get(11)
    assert fetched is not None
    assert fetched.attributes.budget_tier == "unknown"
    assert fetched.attributes.revenue_tier == "unknown"


def test_idempotent_upsert_overwrites_rather_than_duplicates(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    """Same movieid twice -> ONE item; the second write wins (REQ-A-5.5)."""
    repo = _repo(dynamodb_table, config)
    repo.put(enriched_record(movie_id=5, pes_value=10.0, title="First"))
    repo.put(enriched_record(movie_id=5, pes_value=90.0, title="Second"))

    scanned = dynamodb_table.scan()["Items"]
    assert len(scanned) == 1
    fetched = repo.get(5)
    assert fetched is not None
    assert fetched.movie.movie.title == "Second"
    assert fetched.attributes.effectiveness.value == 90.0


def test_query_by_sentiment_ranks_by_pes_descending_numerically(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    """GSI1 query returns positive items ranked by PES DESC, numeric (not lexical)."""
    repo = _repo(dynamodb_table, config)
    # Values chosen so a lexical sort of the raw numbers would mis-order them.
    repo.put(enriched_record(movie_id=1, pes_value=9.50, sentiment=Sentiment.POSITIVE))
    repo.put(enriched_record(movie_id=2, pes_value=100.00, sentiment=Sentiment.POSITIVE))
    repo.put(enriched_record(movie_id=3, pes_value=20.00, sentiment=Sentiment.POSITIVE))
    # A different sentiment must be excluded from the positive query.
    repo.put(enriched_record(movie_id=4, pes_value=99.00, sentiment=Sentiment.NEGATIVE))

    results = repo.query_by_sentiment(Sentiment.POSITIVE)
    assert [r.movie.movie.movie_id for r in results] == [2, 3, 1]
    assert [r.attributes.effectiveness.value for r in results] == [100.0, 20.0, 9.5]
    assert all(r.attributes.overview_sentiment is Sentiment.POSITIVE for r in results)


def test_query_by_sentiment_respects_limit(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    repo = _repo(dynamodb_table, config)
    repo.put(enriched_record(movie_id=1, pes_value=10.0, sentiment=Sentiment.POSITIVE))
    repo.put(enriched_record(movie_id=2, pes_value=50.0, sentiment=Sentiment.POSITIVE))
    repo.put(enriched_record(movie_id=3, pes_value=90.0, sentiment=Sentiment.POSITIVE))
    top = repo.query_by_sentiment(Sentiment.POSITIVE, limit=2)
    assert [r.attributes.effectiveness.value for r in top] == [90.0, 50.0]


def test_batch_get_returns_requested_ids(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    """BatchGetItem for N ids returns exactly those records (REQ-A-5 pattern 2)."""
    repo = _repo(dynamodb_table, config)
    for mid in (10, 20, 30, 40):
        repo.put(enriched_record(movie_id=mid, pes_value=float(mid)))

    results = repo.batch_get([10, 30, 40])
    got_ids = {r.movie.movie.movie_id for r in results}
    assert got_ids == {10, 30, 40}


def test_batch_get_empty_input_returns_empty(
    dynamodb_table: Any, config: PersistenceConfig
) -> None:
    assert _repo(dynamodb_table, config).batch_get([]) == []


def test_scan_numeric_range_filters_budget(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    """Small-N scan filters by a budget range (REQ-A-5 pattern 4)."""
    repo = _repo(dynamodb_table, config)
    repo.put(enriched_record(movie_id=1, pes_value=10.0, budget=1_000_000))
    repo.put(enriched_record(movie_id=2, pes_value=10.0, budget=5_000_000))
    repo.put(enriched_record(movie_id=3, pes_value=10.0, budget=20_000_000))

    results = repo.scan_numeric_range(min_budget=2_000_000, max_budget=10_000_000)
    assert {r.movie.movie.movie_id for r in results} == {2}


def test_scan_numeric_range_filters_revenue_and_runtime(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    repo = _repo(dynamodb_table, config)
    repo.put(enriched_record(movie_id=1, pes_value=10.0, revenue=1_000_000, runtime=90.0))
    repo.put(enriched_record(movie_id=2, pes_value=10.0, revenue=8_000_000, runtime=120.0))
    repo.put(enriched_record(movie_id=3, pes_value=10.0, revenue=8_000_000, runtime=200.0))

    by_revenue = repo.scan_numeric_range(min_revenue=5_000_000)
    assert {r.movie.movie.movie_id for r in by_revenue} == {2, 3}

    by_runtime = repo.scan_numeric_range(min_runtime=100.0, max_runtime=150.0)
    assert {r.movie.movie.movie_id for r in by_runtime} == {2}


def test_scan_numeric_range_no_bounds_returns_all(
    dynamodb_table: Any, config: PersistenceConfig, enriched_record: Any
) -> None:
    repo = _repo(dynamodb_table, config)
    for mid in (1, 2, 3):
        repo.put(enriched_record(movie_id=mid, pes_value=10.0))
    assert len(repo.scan_numeric_range()) == 3
