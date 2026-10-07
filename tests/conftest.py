"""Shared fixtures: real read-only DB paths/connections and synthetic DB builders."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from movieintel.data_access.sqlite_source import open_source

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DB_DIR = _REPO_ROOT / "_sqlite"

MOVIES_DB_PATH = _DB_DIR / "movies.db"
RATINGS_DB_PATH = _DB_DIR / "ratings.db"


@pytest.fixture(scope="session", autouse=True)
def _require_real_dbs() -> None:
    """Fail loudly if the real source DBs are missing (tests run against them)."""
    for path in (MOVIES_DB_PATH, RATINGS_DB_PATH):
        if not path.is_file():
            pytest.fail(f"Required source DB not found: {path}")


@pytest.fixture
def movies_conn() -> Iterator[sqlite3.Connection]:
    conn = open_source(MOVIES_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ratings_conn() -> Iterator[sqlite3.Connection]:
    conn = open_source(RATINGS_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def build_sqlite_db(path: Path, ddl: str, rows: dict[str, list[tuple[object, ...]]]) -> None:
    """Create a small synthetic SQLite DB at ``path`` for negative/edge-case tests.

    ``ddl`` is executed as a script; ``rows`` maps an ``INSERT INTO <table> VALUES``
    target table name to a list of value tuples.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl)
        for table, table_rows in rows.items():
            if not table_rows:
                continue
            placeholders = ", ".join("?" for _ in table_rows[0])
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", table_rows)
        conn.commit()
    finally:
        conn.close()
