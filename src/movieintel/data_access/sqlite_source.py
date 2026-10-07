"""Open SQLite sources read-only and introspect table schemas (REQ-A-1.2, 1.4)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from movieintel.data_access.errors import SourceDatabaseError
from movieintel.data_access.models import ColumnInfo


def open_source(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a SQLite database read-only, failing fast if it is unusable.

    Uses a ``file:...?mode=ro`` URI so writes are rejected by SQLite. Raises
    :class:`SourceDatabaseError` naming the file when it is missing or cannot be
    opened/read (REQ-A-1.4).
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise SourceDatabaseError(str(db_path), "file does not exist")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # Touch the DB so an unreadable/corrupt file fails here, not later.
        conn.execute("SELECT 1")
    except sqlite3.Error as exc:
        raise SourceDatabaseError(str(db_path), str(exc)) from exc
    return conn


def introspect_table(conn: sqlite3.Connection, table: str) -> list[ColumnInfo]:
    """Return columns of ``table`` from ``PRAGMA table_info`` (REQ-A-1.2).

    Returns an empty list when the table does not exist; callers decide whether
    an absent table is an error.
    """
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [
        ColumnInfo(
            cid=int(cid),
            name=str(name),
            type=str(col_type),
            notnull=bool(notnull),
            pk=bool(pk),
        )
        for (cid, name, col_type, notnull, _dflt, pk) in rows
    ]


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQL identifier (PRAGMA does not accept bound parameters)."""
    return '"' + identifier.replace('"', '""') + '"'
