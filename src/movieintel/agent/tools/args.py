"""Per-tool Pydantic args models + structured validation helper (design §3.6, REQ-B-2.5).

Each tool validates its raw arguments against one of these models BEFORE executing. The
models are strict (``extra="forbid"`` ↔ the schema ``additionalProperties: false``) and
their field names, enums, required-ness, and numeric bounds are kept in exact sync with
the Converse ``toolConfig`` ``inputSchema`` dicts in :mod:`specs` — the contract tests
enforce that sync. The models are the single source of truth for accepted arguments.

``validate_args`` runs ``model_validate`` and, on failure, converts the
``pydantic.ValidationError`` into a structured :class:`ToolValidationError` result rather
than letting the exception bubble out, so invalid input never reaches a repository/KB call
(REQ-B-2.5) and every tool returns a uniform typed value.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from movieintel.agent.tools.results import Dimension, SortBy, ToolValidationError
from movieintel.domain.schemas import Sentiment


class QueryMoviesArgs(BaseModel):
    """Validated arguments for ``query_movies`` (schema in :mod:`specs`)."""

    model_config = ConfigDict(extra="forbid")

    sentiment: Sentiment | None = None
    min_budget: float | None = None
    max_budget: float | None = None
    min_revenue: float | None = None
    max_revenue: float | None = None
    min_runtime: float | None = None
    max_runtime: float | None = None
    genres: list[str] | None = None
    sort_by: SortBy | None = None
    limit: Annotated[int, Field(ge=1, le=50)] | None = None


class SemanticSearchArgs(BaseModel):
    """Validated arguments for ``semantic_search`` (schema in :mod:`specs`)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: Annotated[int, Field(ge=1, le=20)] | None = None


class CompareMoviesArgs(BaseModel):
    """Validated arguments for ``compare_movies`` (schema in :mod:`specs`)."""

    model_config = ConfigDict(extra="forbid")

    movie_ids: Annotated[list[str], Field(min_length=2)]
    dimensions: list[Dimension] | None = None


def validate_args[ArgsT: BaseModel](
    tool: str, model: type[ArgsT], raw: dict[str, Any]
) -> ArgsT | ToolValidationError:
    """Validate ``raw`` against ``model``; return the model or a structured error.

    On success returns the validated args model instance. On a
    ``pydantic.ValidationError`` returns a :class:`ToolValidationError` carrying the
    serialized error list, so the caller returns it immediately without executing
    (REQ-B-2.5). Errors are serialized through pydantic's JSON mode so the ``detail``
    payload is plain JSON-safe data (no exception objects, no non-serializable context).
    """
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        return ToolValidationError(tool=tool, detail=exc.errors(include_url=False))
