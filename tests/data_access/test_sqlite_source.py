"""Tests for open_source (fail-fast + read-only) and introspect_table (REQ-A-1.2, 1.4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from movieintel.data_access.errors import SourceDatabaseError
from movieintel.data_access.sqlite_source import introspect_table, open_source


def test_open_source_missing_file_fails_fast(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(SourceDatabaseError) as excinfo:
        open_source(missing)
    assert str(missing) in str(excinfo.value)
    assert excinfo.value.path == str(missing)


def test_open_source_is_read_only(movies_conn: sqlite3.Connection) -> None:
    # Reads work.
    assert movies_conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] > 0
    # Writes are rejected because the connection is mode=ro.
    with pytest.raises(sqlite3.OperationalError):
        movies_conn.execute("CREATE TABLE should_fail (x INTEGER)")


def test_introspect_table_reads_pragma(ratings_conn: sqlite3.Connection) -> None:
    columns = introspect_table(ratings_conn, "ratings")
    names = [c.name for c in columns]
    assert names == ["ratingId", "userId", "movieId", "rating", "timestamp"]
    by_name = {c.name: c for c in columns}
    assert by_name["rating"].type == "REAL"
    assert by_name["ratingId"].pk is True
    assert by_name["userId"].pk is False


def test_introspect_missing_table_returns_empty(ratings_conn: sqlite3.Connection) -> None:
    assert introspect_table(ratings_conn, "does_not_exist") == []
