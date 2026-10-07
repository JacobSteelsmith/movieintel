"""Typed reads over the documented ``movies`` columns (REQ-A-1.1)."""

from __future__ import annotations

import sqlite3

from movieintel.data_access.errors import SourceDatabaseError
from movieintel.data_access.models import MovieRow
from movieintel.data_access.sqlite_source import introspect_table

MOVIES_TABLE = "movies"

#: Logical field -> documented column name (lowercase per the challenge README).
#: Each documented name is resolved case-insensitively against the introspected
#: ``movies`` schema, because the real DB stores camelCase names (e.g. ``movieId``).
MOVIES_DOCUMENTED_COLUMNS: dict[str, str] = {
    "movie_id": "movieid",
    "title": "title",
    "imdb_id": "imdbid",
    "overview": "overview",
    "production_companies": "productioncompanies",
    "release_date": "releasedate",
    "budget": "budget",
    "revenue": "revenue",
    "runtime": "runtime",
    "language": "language",
    "genres": "genres",
    "status": "status",
}


def _resolve_movies_columns(conn: sqlite3.Connection) -> dict[str, str]:
    """Map each logical field to the real (case-correct) ``movies`` column name.

    The documented columns are a hard contract (REQ-A-1.1); a missing one raises
    :class:`SourceDatabaseError` naming the movies table.
    """
    available = {col.name.lower(): col.name for col in introspect_table(conn, MOVIES_TABLE)}
    if not available:
        raise SourceDatabaseError(MOVIES_TABLE, "movies table not found")
    resolved: dict[str, str] = {}
    for logical, documented in MOVIES_DOCUMENTED_COLUMNS.items():
        real = available.get(documented.lower())
        if real is None:
            raise SourceDatabaseError(MOVIES_TABLE, f"documented column '{documented}' is absent")
        resolved[logical] = real
    return resolved


def read_movies(conn: sqlite3.Connection, limit: int) -> list[MovieRow]:
    """Read the first ``limit`` movies over the documented columns (REQ-A-1.1).

    Columns are resolved case-insensitively against the introspected schema and
    ordered by the movie key for a stable projection.
    """
    resolved = _resolve_movies_columns(conn)
    return _read_movie_rows(conn, resolved, where_clause=None, params=(), limit=limit)


def read_movies_by_ids(
    conn: sqlite3.Connection,
    movie_ids: list[int],
) -> list[MovieRow]:
    """Read the movies whose key is in ``movie_ids`` (used by sampling)."""
    if not movie_ids:
        return []
    resolved = _resolve_movies_columns(conn)
    placeholders = ", ".join("?" for _ in movie_ids)
    key_col = _quote_identifier(resolved["movie_id"])
    where = f"{key_col} IN ({placeholders})"
    return _read_movie_rows(conn, resolved, where_clause=where, params=tuple(movie_ids), limit=None)


def _read_movie_rows(
    conn: sqlite3.Connection,
    resolved: dict[str, str],
    *,
    where_clause: str | None,
    params: tuple[object, ...],
    limit: int | None,
) -> list[MovieRow]:
    logical_order = list(MOVIES_DOCUMENTED_COLUMNS)
    projection = ", ".join(_quote_identifier(resolved[field]) for field in logical_order)
    key_col = _quote_identifier(resolved["movie_id"])
    sql = f"SELECT {projection} FROM {_quote_identifier(MOVIES_TABLE)}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    sql += f" ORDER BY {key_col}"
    query_params = params
    if limit is not None:
        sql += " LIMIT ?"
        query_params = (*params, limit)
    rows = conn.execute(sql, query_params).fetchall()
    return [MovieRow(**dict(zip(logical_order, row, strict=True))) for row in rows]


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQL identifier (resolved from introspection, not user input)."""
    return '"' + identifier.replace('"', '""') + '"'
