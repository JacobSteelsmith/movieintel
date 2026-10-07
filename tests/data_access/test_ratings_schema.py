"""Tests for ratings role resolution and derived rating scale (REQ-A-1.2, 1.5, 1.6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from movieintel.data_access.errors import MissingRatingsColumnError
from movieintel.data_access.ratings import (
    observe_rating_scale,
    resolve_ratings_schema,
)
from movieintel.data_access.sqlite_source import open_source
from tests.conftest import build_sqlite_db


def test_resolve_ratings_schema_on_real_db(ratings_conn: sqlite3.Connection) -> None:
    schema = resolve_ratings_schema(ratings_conn)
    assert schema.movie_key_column == "movieId"
    assert schema.rating_column == "rating"
    assert schema.all_columns == ["ratingId", "userId", "movieId", "rating", "timestamp"]


def test_missing_rating_role_raises(tmp_path: Path) -> None:
    db = tmp_path / "ratings_norating.db"
    build_sqlite_db(
        db,
        "CREATE TABLE ratings (movieId INTEGER, note TEXT);",
        {"ratings": [(1, "a"), (2, "b")]},
    )
    conn = open_source(db)
    try:
        with pytest.raises(MissingRatingsColumnError) as excinfo:
            resolve_ratings_schema(conn)
    finally:
        conn.close()
    assert excinfo.value.role == "rating"
    assert excinfo.value.available_columns == ["movieId", "note"]


def test_candidate_ranking_is_case_insensitive(tmp_path: Path) -> None:
    db = tmp_path / "ratings_alt.db"
    build_sqlite_db(
        db,
        "CREATE TABLE ratings (MID INTEGER, Score REAL);",
        {"ratings": [(1, 4.0), (2, 2.0)]},
    )
    conn = open_source(db)
    try:
        schema = resolve_ratings_schema(conn)
    finally:
        conn.close()
    assert schema.movie_key_column == "MID"
    assert schema.rating_column == "Score"


def test_observe_rating_scale_from_real_data(ratings_conn: sqlite3.Connection) -> None:
    schema = resolve_ratings_schema(ratings_conn)
    scale = observe_rating_scale(ratings_conn, schema)
    assert scale.observed_min == 0.5
    assert scale.observed_max == 5.0
    assert scale.declared_max == 5.0
    assert scale.effective_max == 5.0
    assert scale.fallback_used is False


def test_observe_rating_scale_fallback_is_loud(tmp_path: Path) -> None:
    db = tmp_path / "ratings_empty.db"
    build_sqlite_db(
        db,
        "CREATE TABLE ratings (movieId INTEGER, rating REAL);",
        {"ratings": []},
    )
    conn = open_source(db)
    try:
        schema = resolve_ratings_schema(conn)
        scale = observe_rating_scale(conn, schema, fallback_default=5.0)
    finally:
        conn.close()
    assert scale.observed_min is None
    assert scale.observed_max is None
    assert scale.fallback_used is True
    assert scale.effective_max == 5.0
