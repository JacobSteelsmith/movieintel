"""Typed models for the data-access layer (design §5).

``ColumnInfo`` is a frozen dataclass because it is a direct projection of a
``PRAGMA table_info`` row. The remaining models are Pydantic v2 models so the
project gets free validation and accept/reject tests (seeds REQ-X-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, field_validator


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """A single column as reported by ``PRAGMA table_info(<table>)``.

    Built from a PRAGMA row ``(cid, name, type, notnull, dflt_value, pk)``.
    """

    cid: int
    name: str
    type: str
    notnull: bool
    pk: bool


class MovieRow(BaseModel):
    """A typed row over the documented ``movies`` columns (REQ-A-1.1).

    ``production_companies`` and ``genres`` are kept as the raw JSON-array text
    exactly as stored; parsing is a later concern, not Task 1.
    """

    movie_id: int
    title: str
    imdb_id: str | None = None
    overview: str | None = None
    production_companies: str | None = None
    release_date: str | None = None
    budget: int | None = None
    revenue: int | None = None
    runtime: float | None = None
    language: str | None = None
    genres: str | None = None
    status: str | None = None

    @field_validator("budget", "revenue", "runtime", mode="before")
    @classmethod
    def _blank_numeric_to_none(cls, value: Any) -> Any:
        """Treat empty/whitespace strings in numeric source columns as missing.

        The real ``movies`` DB stores some numeric fields (e.g. ``runtime``) as
        empty strings; coerce them to ``None`` rather than failing a read, so a
        missing value is represented honestly instead of fabricated.
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class RatingsSchema(BaseModel):
    """Roles resolved from the introspected ``ratings`` schema (REQ-A-1.2)."""

    movie_key_column: str
    rating_column: str
    all_columns: list[str]


class RatingScale(BaseModel):
    """Rating scale/range derived from the ratings data (REQ-A-1.6).

    ``effective_max`` is the observed maximum when present, otherwise the
    ``fallback_default``; in the latter case ``fallback_used`` is True so the
    assumption is surfaced loudly rather than applied silently (REQ-A-3.1).
    """

    observed_min: float | None
    observed_max: float | None
    declared_max: float = 5.0
    effective_max: float
    fallback_used: bool = False
    fallback_default: float = 5.0


class SampledMovie(BaseModel):
    """A sampled movie left-joined to its aggregated rating (REQ-A-1.3).

    ``rating`` is ``AVG(rating)`` for the movie because the ``ratings`` table is
    per-user; ``rating``/``rating_count`` are ``None``/``0`` for movies with no
    matching ratings row (the left join tolerates missing ratings).
    """

    movie: MovieRow
    rating: float | None = None
    rating_count: int = 0
