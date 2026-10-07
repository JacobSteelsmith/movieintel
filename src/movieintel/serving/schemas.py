"""Serving request + validation-error models (Task 10 FEAT-001, REQ-B-4.1/.4).

:class:`QueryRequest` is the strict (``extra="forbid"``) body of the POST ``/query``
endpoint: a required non-empty ``query`` plus an optional, bounded ``max_turns`` override.
:class:`ValidationErrorResponse` is the structured 400 body returned when the request body
violates that schema; it mirrors :class:`movieintel.agent.response.FinalAnswerError` - a
``Literal`` ``error`` discriminator plus a ``detail`` list built from
``ValidationError.errors(include_url=False)``.

The success body is NOT wrapped here: a valid request is answered with a serialized
``AgentResult`` (``movieintel.agent.response``) exactly as the loop returns it. These
models are the single source of truth for the generated ``docs/openapi.yaml``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Documented upper cap on the per-request ``max_turns`` override. The agent loop's own
# default lives in ``movieintel.agent.loop.MAX_TURNS``; this cap bounds how high a caller
# may raise it so a single request cannot run the loop unbounded. Overriding below this
# cap (and at or above the floor of 1) is accepted.
MAX_TURNS_CAP = 12


class QueryRequest(BaseModel):
    """The POST ``/query`` request body (REQ-B-4.1).

    ``query`` is the required natural-language request (non-empty). ``max_turns`` is an
    optional override of the agent loop's turn bound, constrained to ``[1, MAX_TURNS_CAP]``;
    when omitted (``None``) the handler falls back to the loop default.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    max_turns: int | None = Field(default=None, ge=1, le=MAX_TURNS_CAP)


class ValidationErrorResponse(BaseModel):
    """Structured 400 body for a schema-violating request (REQ-B-4.4, REQ-X-3.3).

    Mirrors :class:`movieintel.agent.response.FinalAnswerError`: a literal ``error``
    discriminator and a ``detail`` list carrying the Pydantic/JSON error entries from
    ``ValidationError.errors(include_url=False)`` (or an equivalent decode-error entry).
    """

    model_config = ConfigDict(extra="forbid")

    error: Literal["invalid_request"] = "invalid_request"
    detail: list[dict[str, Any]]
