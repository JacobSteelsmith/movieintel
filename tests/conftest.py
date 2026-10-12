"""Shared fixtures: real read-only DB paths/connections and synthetic DB builders."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from movieintel.data_access.sqlite_source import open_source

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DB_DIR = _REPO_ROOT / "_sqlite"

MOVIES_DB_PATH = _DB_DIR / "movies.db"
RATINGS_DB_PATH = _DB_DIR / "ratings.db"


def _missing_source_dbs() -> list[Path]:
    """Return the real source DBs that are absent (empty when both are present)."""
    return [path for path in (MOVIES_DB_PATH, RATINGS_DB_PATH) if not path.is_file()]


def skip_or_fail_if_dbs_missing() -> None:
    """Skip on CI (or fail locally) when the real source DBs are absent.

    The source DBs (``_sqlite/*.db``) are intentionally never committed - they are
    gitignored and shipped out of band (a Lambda layer in production). So they are
    present for a local developer run but absent on CI.

    - On CI (``CI`` env var set, e.g. GitHub Actions), skip when the DBs are missing:
      there is nothing to run against, and failing would be noise.
    - Locally (no ``CI`` env var), where the DBs are expected, fail loudly so a developer
      does not silently skip real-DB coverage.

    Shared by the ``require_real_dbs`` fixture (per-test gating) and by module-level
    ``pytestmark`` guards (e.g. the pipeline synth tests, which all build the SQLite
    Lambda layer from the real ``_sqlite/`` dir).
    """
    missing = _missing_source_dbs()
    if not missing:
        return
    names = ", ".join(str(path) for path in missing)
    if os.environ.get("CI"):
        pytest.skip(f"Source DB(s) not available on CI; skipping real-DB test: {names}")
    pytest.fail(
        f"Required source DB not found: {names}. These live in the gitignored _sqlite/ "
        "and are expected for a local run. Restore them, or set CI=1 to skip real-DB tests."
    )


@pytest.fixture
def require_real_dbs() -> None:
    """Per-test gate for tests that read the real read-only source DBs.

    Only tests that request this fixture (directly or via ``movies_conn``/``ratings_conn``)
    are affected; synthetic-DB tests built with ``build_sqlite_db`` run unconditionally.
    See :func:`skip_or_fail_if_dbs_missing` for the CI-skip / local-fail policy.
    """
    skip_or_fail_if_dbs_missing()


@pytest.fixture
def movies_conn(require_real_dbs: None) -> Iterator[sqlite3.Connection]:
    conn = open_source(MOVIES_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ratings_conn(require_real_dbs: None) -> Iterator[sqlite3.Connection]:
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
