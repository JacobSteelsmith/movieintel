"""Async-job serving models (async 202 + poll path, design Decision 1).

Strict (``extra="forbid"``) Pydantic models for the submit-then-poll contract on the
existing HTTP API: the submit 202 body (:class:`SubmitResponse`), the poll 200 body
(:class:`JobStatusResponse`), the per-phase :class:`JobProgress`, the structured terminal
:class:`WorkerError`, and the poll-miss :class:`NotFoundResponse`. These are the single
source of truth for the generated OpenAPI contract.

:class:`JobStatusResponse` embeds the agent's five-kind ``AgentResult`` union VERBATIM as
its optional ``result`` field - the first use of that PEP 695 ``type`` alias as a Pydantic
field. Pydantic v2 resolves the alias and validates ``result`` via smart-union on the
disjoint ``kind`` discriminator (no ``TypeAdapter``, no explicit ``Field(discriminator=...)``).
``result``/``error`` are serialized only when terminal (callers dump with ``exclude_none=True``);
a ``failed`` body INTENTIONALLY nests the self-describing :class:`WorkerError` under the
top-level ``error`` key (design Finding 8) - do not flatten it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from movieintel.agent.progress import ProgressPhase
from movieintel.agent.response import AgentResult

__all__ = [
    "JobProgress",
    "JobStatus",
    "JobStatusResponse",
    "NotFoundResponse",
    "SubmitResponse",
    "WorkerError",
]


class JobStatus(StrEnum):
    """The lifecycle state of an async job (design Decision 1)."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class JobProgress(BaseModel):
    """Real per-phase progress of the agent loop for a job (design FR-3)."""

    model_config = ConfigDict(extra="forbid")

    phase: ProgressPhase
    turn: int
    label: str


class SubmitResponse(BaseModel):
    """The POST ``/jobs`` 202 body: a job id, ``queued`` status, and a poll URL."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["queued"] = "queued"
    poll_url: str


class WorkerError(BaseModel):
    """Structured terminal error for a ``failed`` job (worker crash, not a modeled outcome)."""

    model_config = ConfigDict(extra="forbid")

    error: Literal["worker_failed"] = "worker_failed"
    message: str


class NotFoundResponse(BaseModel):
    """The 404 body for an unknown/expired job id on the poll route."""

    model_config = ConfigDict(extra="forbid")

    error: Literal["not_found"] = "not_found"
    message: str


class JobStatusResponse(BaseModel):
    """The GET ``/jobs/{id}`` 200 body (design Decision 1).

    ``progress`` is REQUIRED (every write path preserves a complete progress map). ``result``
    is the serialized five-kind ``AgentResult`` on success; ``error`` is a nested
    :class:`WorkerError` on failure. Both are ``None`` while in-flight and are omitted on the
    wire via ``exclude_none=True`` at the serialize site.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    progress: JobProgress
    created_at: str
    updated_at: str
    result: AgentResult | None = None
    error: WorkerError | None = None
