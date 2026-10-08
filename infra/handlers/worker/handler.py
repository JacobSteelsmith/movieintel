"""Async worker handler (async 202 + poll path, design Decision 3).

Invoked asynchronously (``InvocationType='Event'``) by the submit handler with a TRUSTED
event ``{job_id, query, max_turns}``. It runs ``run_agent`` to completion, streams real
per-phase progress into the job record via a ``(phase, turn)``-throttled ``on_progress``
closure, and writes exactly one terminal record. A single, deliberate broad ``except`` (the
ONLY one allowed) guarantees FR-4: a terminal ``failed`` record always exists even on an
unexpected infrastructure error, so a poller never hangs.

Dependencies are injectable (keyword-only) and default to the shared lazy boto3 builders so
the module imports cleanly without AWS creds and tests inject fakes + a scripted Converse
client.
"""

from __future__ import annotations

import logging
from typing import Any

from handlers.shared.serving import (
    _default_bedrock_client,
    _default_kb_client,
    _default_repository,
)
from movieintel.agent.loop import MAX_TURNS, run_agent
from movieintel.agent.progress import ProgressEvent
from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
from movieintel.persistence.config import PersistenceConfig
from movieintel.serving.jobs import JobProgress, WorkerError
from movieintel.serving.jobs_store import JobStore

logger = logging.getLogger(__name__)


def _default_job_store(persistence: PersistenceConfig) -> JobStore:
    """Build the live :class:`JobStore` over the configured DynamoDB table (lazy boto3)."""
    import boto3

    table = boto3.resource("dynamodb", region_name=persistence.region).Table(persistence.table_name)
    return JobStore(table=table, config=persistence)


def _progress_writer(job_store: JobStore, job_id: str) -> Any:
    """Build a ``(phase, turn)``-throttled ``on_progress`` closure over ``job_store``.

    Writes on the FIRST event or whenever the incoming ``(phase, turn)`` pair differs from
    the last written pair, so the user-facing turn stays current while writes are bounded to
    about one per turn-or-phase transition (collapsing same-turn duplicate tool blocks).
    """
    last: list[tuple[str, int] | None] = [None]

    def on_progress(event: ProgressEvent) -> None:
        pair = (event.phase.value, event.turn)
        if last[0] == pair:
            return
        last[0] = pair
        job_store.update_progress(
            job_id, JobProgress(phase=event.phase, turn=event.turn, label=event.label)
        )

    return on_progress


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    client: Any | None = None,
    repository: Any | None = None,
    kb_client: Any | None = None,
    config: BedrockConfig | None = None,
    persistence: PersistenceConfig | None = None,
    kb_config: KBConfig | None = None,
    job_store: JobStore | None = None,
) -> dict[str, Any]:
    """Run ``run_agent`` to completion for the job and write a guaranteed terminal record."""
    job_id = event.get("job_id")
    if not job_id:
        # Nothing to write without a job id (trusted event; our own submit handler builds it).
        logger.error("worker event missing job_id; nothing to do")
        return {"job_id": None}

    query = event["query"]
    max_turns = event.get("max_turns")

    config = config or BedrockConfig.from_env()
    persistence = persistence or PersistenceConfig.from_env()
    kb_config = kb_config or KBConfig.from_env()
    client = client or _default_bedrock_client(config)
    repository = repository or _default_repository(persistence)
    kb_client = kb_client or _default_kb_client(kb_config)
    job_store = job_store or _default_job_store(persistence)

    job_store.mark_running(job_id)
    try:
        result = run_agent(
            query,
            client=client,
            repository=repository,
            kb_client=kb_client,
            kb_config=kb_config,
            config=config,
            max_turns=max_turns or MAX_TURNS,
            on_progress=_progress_writer(job_store, job_id),
        )
        job_store.mark_succeeded(job_id, result)
    except Exception:  # noqa: BLE001 - last-resort guard; a terminal record MUST exist (FR-4)
        logger.exception("worker failed for job %s", job_id)
        job_store.mark_failed(job_id, WorkerError(message="unexpected error running the agent"))
    return {"job_id": job_id}
