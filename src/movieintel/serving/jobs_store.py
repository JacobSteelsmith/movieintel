"""DynamoDB-backed async-job store (design Decision 2).

A thin wrapper over an injected boto3 DynamoDB ``Table`` resource (same injection pattern
as :class:`~movieintel.persistence.repository.MovieIntelRepository`) that stores async jobs
under a dedicated ``JOB#`` keyspace in the existing single ``MovieIntel`` table. The submit
handler and worker share this one tested API; tests drive it over a moto table.

Serialization is a SELF-CONTAINED generic JSON round trip (``_to_dynamo``/``_from_dynamo``)
rather than the movie-specific field-by-field serializer: DynamoDB's ``Table`` resource
rejects Python ``float`` (it needs ``Decimal``), and the embedded ``AgentResult`` map is
arbitrarily shaped (it can carry a ``pes`` float), so a model -> JSON -> ``Decimal`` round
trip (and the inverse on read) keeps DynamoDB happy while the Pydantic models stay
authoritative.

Every mutating method is guarded by ``attribute_exists(PK) AND status IN (queued, running)``
so no write can resurrect or overwrite a terminal record: a progress write that loses the
race is advisory and swallowed at ``warning``; a terminal mark that loses it is an idempotent
set and tolerated. The read path NEVER validates the raw item (it carries ``PK``/``SK``/
``query``/``max_turns``/``expires_at`` and stores no ``job_id``); it builds an explicit
projected payload of only model fields and reconstructs ``job_id`` from the key.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from movieintel.agent.progress import PHASE_LABELS, ProgressPhase
from movieintel.agent.response import AgentResult
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.keys import job_pk, job_sk
from movieintel.serving.jobs import (
    JobProgress,
    JobStatus,
    JobStatusResponse,
    WorkerError,
)

logger = logging.getLogger(__name__)

#: Job records expire from DynamoDB (via the ``expires_at`` TTL attribute) after 24h.
JOB_TTL_SECONDS = 86400

# Shared ConditionExpression + attribute names for every mutating write: a job may only be
# written while it still exists and is non-terminal (status in {queued, running}).
_MUTATE_CONDITION = "attribute_exists(PK) AND #s IN (:queued,:running)"
_STATUS_NAME = {"#s": "status"}


def _to_dynamo(model: BaseModel) -> dict[str, Any]:
    """Serialize a Pydantic model to a DynamoDB-safe map (floats -> ``Decimal``).

    pydantic -> JSON-safe dict -> JSON text -> reload coercing every float to ``Decimal``
    (the ``Table`` resource rejects native ``float``).
    """
    dumped: dict[str, Any] = json.loads(
        json.dumps(model.model_dump(mode="json")), parse_float=Decimal
    )
    return dumped


def _from_dynamo(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a DynamoDB map back to JSON-native numbers (``Decimal`` -> float/int).

    ``Decimal`` is not JSON-serializable by default; ``default=float`` renders each one, and
    reloading restores JSON-native numbers so no stray ``Decimal`` reaches a Pydantic model.
    """
    normalized: dict[str, Any] = json.loads(json.dumps(raw, default=float))
    return normalized


def _now_iso() -> str:
    """Current time as an ISO-8601 UTC string with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobStore:
    """CRUD for async-job items in the ``MovieIntel`` table's ``JOB#`` keyspace."""

    def __init__(self, *, table: Any, config: PersistenceConfig) -> None:
        self._table = table
        self._config = config

    def create(self, *, job_id: str, query: str, max_turns: int | None) -> None:
        """Write the initial ``queued`` record with a COMPLETE progress map.

        ``ConditionExpression=attribute_not_exists(PK)`` so a duplicate id cannot clobber an
        existing job. The initial progress is ``understanding/0`` so any later read (even of
        a job whose worker never advanced) always carries a complete :class:`JobProgress`.
        """
        now = _now_iso()
        item: dict[str, Any] = {
            "PK": job_pk(job_id),
            "SK": job_sk(),
            "status": JobStatus.queued.value,
            "progress": _to_dynamo(
                JobProgress(
                    phase=ProgressPhase.understanding,
                    turn=0,
                    label=PHASE_LABELS[ProgressPhase.understanding],
                )
            ),
            "query": query,
            "created_at": now,
            "updated_at": now,
            "expires_at": int(time.time()) + JOB_TTL_SECONDS,
        }
        if max_turns is not None:
            item["max_turns"] = max_turns
        self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")

    def mark_running(self, job_id: str) -> None:
        """Flip ``queued -> running`` (the SOLE owner of this transition); set ``updated_at``.

        Idempotent via the shared non-terminal guard: a second call (or a call on an
        already-``running`` job) is accepted, but it can never reverse a terminal record.
        """
        self._guarded_update(
            job_id,
            update="SET #s = :running, updated_at = :t",
            names={**_STATUS_NAME},
            values={
                ":running": JobStatus.running.value,
                ":t": _now_iso(),
                ":queued": JobStatus.queued.value,
            },
        )

    def update_progress(self, job_id: str, progress: JobProgress) -> None:
        """Overwrite ``progress`` (and ``updated_at``); never touches ``status``.

        Progress is advisory: a conditional-check failure (the job went terminal first) is
        caught and swallowed at ``warning`` so a stray late write never fails the worker.
        """
        try:
            self._guarded_update(
                job_id,
                update="SET progress = :p, updated_at = :t",
                values={":p": _to_dynamo(progress), ":t": _now_iso()},
            )
        except Exception:  # noqa: BLE001 - progress is advisory, swallow a lost-race write
            logger.warning(
                "progress update for job %s was rejected (likely already terminal)",
                job_id,
                exc_info=True,
            )

    def mark_succeeded(self, job_id: str, result: AgentResult) -> None:
        """Terminal write: set ``status=succeeded`` + the serialized ``result``.

        A conditional-check failure means the job was already terminal; the terminal set is
        idempotent, so it is tolerated (swallowed at ``warning``).
        """
        self._terminal_update(
            job_id,
            update="SET #s = :succeeded, #r = :r, updated_at = :t",
            names={**_STATUS_NAME, "#r": "result"},
            values={
                ":succeeded": JobStatus.succeeded.value,
                ":r": _to_dynamo(result),
                ":t": _now_iso(),
            },
        )

    def mark_failed(self, job_id: str, error: WorkerError) -> None:
        """Terminal write: set ``status=failed`` + the structured ``error``.

        Tolerant of a conditional-check failure (already terminal) for the same reason as
        :meth:`mark_succeeded`.
        """
        self._terminal_update(
            job_id,
            update="SET #s = :failed, #e = :e, updated_at = :t",
            names={**_STATUS_NAME, "#e": "error"},
            values={
                ":failed": JobStatus.failed.value,
                ":e": _to_dynamo(error),
                ":t": _now_iso(),
            },
        )

    def get(self, job_id: str) -> JobStatusResponse | None:
        """Read a job by id as a :class:`JobStatusResponse`; ``None`` when absent.

        Builds an EXPLICIT PROJECTED payload of ONLY model fields - reconstructing ``job_id``
        from the ARGUMENT (the item stores none) and routing ``progress``/``result``/``error``
        through :func:`_from_dynamo` so no stray ``Decimal`` reaches the model - then validates
        THAT payload. The item's ``PK``/``SK``/``query``/``max_turns``/``expires_at`` are
        excluded, so ``extra='forbid'`` is safe.
        """
        resp = self._table.get_item(Key={"PK": job_pk(job_id), "SK": job_sk()})
        item = resp.get("Item")
        if item is None:
            return None
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": item["status"],
            "progress": _from_dynamo(item["progress"]),
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }
        if "result" in item:
            payload["result"] = _from_dynamo(item["result"])
        if "error" in item:
            payload["error"] = _from_dynamo(item["error"])
        return JobStatusResponse.model_validate(payload)

    def _guarded_update(
        self,
        job_id: str,
        *,
        update: str,
        values: dict[str, Any],
        names: dict[str, str] | None = None,
    ) -> None:
        """UpdateItem under the shared non-terminal guard (raises on a conditional failure)."""
        self._table.update_item(
            Key={"PK": job_pk(job_id), "SK": job_sk()},
            UpdateExpression=update,
            ConditionExpression=_MUTATE_CONDITION,
            ExpressionAttributeNames={**_STATUS_NAME, **(names or {})},
            ExpressionAttributeValues={
                **values,
                ":queued": JobStatus.queued.value,
                ":running": JobStatus.running.value,
            },
        )

    def _terminal_update(
        self,
        job_id: str,
        *,
        update: str,
        values: dict[str, Any],
        names: dict[str, str],
    ) -> None:
        """A guarded terminal write that tolerates an already-terminal (idempotent) record."""
        try:
            self._guarded_update(job_id, update=update, values=values, names=names)
        except Exception:  # noqa: BLE001 - terminal set is idempotent; already-terminal is fine
            logger.warning(
                "terminal mark for job %s was rejected (likely already terminal)",
                job_id,
                exc_info=True,
            )
