"""Model construction/validation accept-reject tests (seeds REQ-X-1, REQ-X-3.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movieintel.data_access.models import (
    ColumnInfo,
    MovieRow,
    RatingScale,
    RatingsSchema,
    SampledMovie,
)


def test_columninfo_is_frozen() -> None:
    col = ColumnInfo(cid=0, name="movieId", type="INTEGER", notnull=False, pk=True)
    assert col.name == "movieId"
    with pytest.raises(AttributeError):
        col.name = "other"  # type: ignore[misc]


def test_movierow_accepts_well_formed() -> None:
    row = MovieRow(movie_id=1, title="A Movie", budget=1000, runtime=90.0)
    assert row.movie_id == 1
    assert row.imdb_id is None


def test_movierow_rejects_bad_types() -> None:
    with pytest.raises(ValidationError):
        MovieRow(movie_id="not-an-int", title="X")
    with pytest.raises(ValidationError):
        MovieRow(movie_id=1, title="X", budget="lots")


def test_ratingscale_roundtrip() -> None:
    scale = RatingScale(observed_min=0.5, observed_max=5.0, effective_max=5.0, fallback_used=False)
    assert scale.declared_max == 5.0
    assert scale.fallback_default == 5.0


def test_sampledmovie_defaults() -> None:
    movie = MovieRow(movie_id=1, title="X")
    sampled = SampledMovie(movie=movie)
    assert sampled.rating is None
    assert sampled.rating_count == 0


def test_ratings_schema_requires_columns() -> None:
    schema = RatingsSchema(
        movie_key_column="movieId", rating_column="rating", all_columns=["movieId", "rating"]
    )
    assert schema.movie_key_column == "movieId"
    with pytest.raises(ValidationError):
        RatingsSchema(movie_key_column="movieId", rating_column="rating")  # type: ignore[call-arg]
