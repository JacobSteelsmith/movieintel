"""Pydantic result models for the three agent tools (design §3.6, REQ-X-1.1).

Every tool returns a Pydantic-validated result object. The models reuse the domain enums
(:class:`Sentiment`, :class:`Mood`, :class:`PESTier`) rather than redefining them, and are
strict (``extra="forbid"``) in line with the rest of the codebase.

``ToolValidationError`` is the structured error a tool returns when its raw arguments fail
validation, BEFORE any repository/KB call (REQ-B-2.5). It is a result object, not a raised
exception, so callers get a uniform typed return.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from movieintel.domain.schemas import Mood, PESTier, Sentiment

# The comparable numeric dimensions, shared by the compare_movies args + result (§3.6).
Dimension = Literal["budget", "revenue", "runtime", "pes"]
SortBy = Literal["pes", "revenue", "budget", "runtime"]
# Ordering direction for query_movies: "desc" (default, highest first) or "asc" (lowest first).
SortDirection = Literal["asc", "desc"]


class ToolValidationError(BaseModel):
    """Structured validation error returned (not raised) when tool args are invalid.

    Returned BEFORE any repository/KB call so an invalid request never executes
    (REQ-B-2.5). ``detail`` carries the serialized ``pydantic.ValidationError.errors()``
    list (loc/msg/type), so callers see exactly which argument failed and why.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    error: str = "invalid_arguments"
    detail: list[dict[str, Any]]


class QueryMoviesMovie(BaseModel):
    """One row of the ``query_movies`` structured result."""

    model_config = ConfigDict(extra="forbid")

    movie_id: str
    title: str
    genres: str | None
    sentiment: Sentiment
    budget: int | None
    revenue: int | None
    runtime: float | None
    pes: float
    pes_tier: PESTier | None
    mood: Mood


class QueryMoviesResult(BaseModel):
    """Structured result of ``query_movies`` (design §3.6)."""

    model_config = ConfigDict(extra="forbid")

    count: int
    sort_by: SortBy
    used_index: bool
    movies: list[QueryMoviesMovie]


class SemanticSearchHit(BaseModel):
    """One semantic-search hit mapped 1:1 from a ``kb.client.RetrievedMovie``."""

    model_config = ConfigDict(extra="forbid")

    text: str
    score: float | None
    title: str | None
    genres: str | None
    sentiment: Sentiment | None
    mood: Mood | None
    pes: float | None


class SemanticSearchResult(BaseModel):
    """Structured result of ``semantic_search`` (design §3.6)."""

    model_config = ConfigDict(extra="forbid")

    query: str
    count: int
    hits: list[SemanticSearchHit]


class DimensionComparison(BaseModel):
    """Per-dimension side-by-side comparison for ``compare_movies``.

    ``values`` maps each requested ``movie_id`` to its value for this dimension (``None``
    when the movie is missing a value — never fabricated). ``max_movie_id`` /
    ``min_movie_id`` are chosen over the defined values only, with ties broken by the
    order the ids were requested; both are ``None`` when no movie has a defined value.
    """

    model_config = ConfigDict(extra="forbid")

    dimension: Dimension
    values: dict[str, float | None]
    max_movie_id: str | None
    min_movie_id: str | None
    defined_count: int


class CompareMoviesMovie(BaseModel):
    """One movie in the ``compare_movies`` result; ``found`` is False when batch_get missed it."""

    model_config = ConfigDict(extra="forbid")

    movie_id: str
    title: str
    found: bool


class CompareMoviesResult(BaseModel):
    """Structured result of ``compare_movies`` (design §3.6)."""

    model_config = ConfigDict(extra="forbid")

    movies: list[CompareMoviesMovie]
    missing_ids: list[str]
    comparisons: list[DimensionComparison]
