"""Async-job submit + poll handler (async 202 + poll path, design Decision 1 & 3).

One Lambda backs BOTH ``POST /jobs`` (submit) and ``GET /jobs/{id}`` (poll) on the existing
HTTP API, mirroring how the serve handler backs ``POST /query`` + ``GET /health``:

- **Submit** validates the body with the SAME strictness as ``POST /query`` (reusing the
  shared ``_parse_request``), writes a ``queued`` job record BEFORE dispatch, asynchronously
  invokes the worker (``InvocationType='Event'``), and returns ``202`` well under 30s. A bad
  body returns ``400`` with no record and no invoke; a ``JobStore.create`` failure returns
  ``500``; an async-invoke failure marks the job ``failed`` but STILL returns ``202`` (the
  client has a job id to poll a uniform terminal path).
- **Poll** reads one item: ``None`` -> ``404``, found -> ``200``, a read that RAISES ->
  ``500 poll_failed``.

Dependencies are injectable (keyword-only) and default to lazy boto3-backed builders so the
module imports cleanly without AWS creds and tests inject fakes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from handlers.shared.serving import (
    _JSON_HEADERS,
    _bad_request,
    _decode_error,
    _parse_request,
)
from movieintel.persistence.config import PersistenceConfig
from movieintel.serving.jobs import NotFoundResponse, SubmitResponse, WorkerError
from movieintel.serving.jobs_store import JobStore
from movieintel.serving.schemas import ValidationErrorResponse

logger = logging.getLogger(__name__)


def _default_job_store(persistence: PersistenceConfig) -> JobStore:
    """Build the live :class:`JobStore` over the configured DynamoDB table (lazy boto3)."""
    import boto3

    table = boto3.resource("dynamodb", region_name=persistence.region).Table(persistence.table_name)
    return JobStore(table=table, config=persistence)


def _default_lambda_client(persistence: PersistenceConfig) -> Any:
    """Build the live ``lambda`` client used to async-invoke the worker (lazy boto3)."""
    import boto3

    return boto3.client("lambda", region_name=persistence.region)


def _is_submit_request(event: dict[str, Any]) -> bool:
    """True for ``POST /jobs`` (routeKey template OR the resolved v2 method+path)."""
    if event.get("routeKey") == "POST /jobs":
        return True
    http = event.get("requestContext", {}).get("http", {})
    return bool(http.get("method") == "POST" and http.get("path") == "/jobs")


def _is_poll_request(event: dict[str, Any]) -> bool:
    """True for ``GET /jobs/{id}`` (routeKey template OR the resolved ``/jobs/<id>`` path)."""
    if event.get("routeKey") == "GET /jobs/{id}":
        return True
    http = event.get("requestContext", {}).get("http", {})
    path = http.get("path") or ""
    return http.get("method") == "GET" and path.startswith("/jobs/")


def _job_id_from_event(event: dict[str, Any]) -> str:
    """Resolve the poll ``{id}`` from ``pathParameters.id`` or the last resolved-path segment.

    The defensive fallback parses the LAST segment of the RESOLVED path (never a comparison
    against the ``{id}`` template). An empty/missing id yields ``""`` (treated as 404).
    """
    params = event.get("pathParameters") or {}
    job_id = params.get("id")
    if job_id:
        return str(job_id)
    path = event.get("requestContext", {}).get("http", {}).get("path") or ""
    return path.rstrip("/").rsplit("/", 1)[-1]


def _not_found() -> dict[str, Any]:
    """Build the 404 ``NotFoundResponse`` API-Gateway-proxy response."""
    return {
        "statusCode": 404,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(NotFoundResponse(message="no job with that id").model_dump(mode="json")),
    }


def _error_response(status: int, error: str, message: str) -> dict[str, Any]:
    """Build a structured ``{error, message}`` API-Gateway-proxy response at ``status``."""
    return {
        "statusCode": status,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps({"error": error, "message": message}),
    }


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    job_store: JobStore | None = None,
    lambda_client: Any | None = None,
    config: PersistenceConfig | None = None,
    persistence: PersistenceConfig | None = None,
    kb_config: Any | None = None,
) -> dict[str, Any]:
    """Dispatch ``POST /jobs`` (submit) / ``GET /jobs/{id}`` (poll); else ``404``."""
    if _is_submit_request(event):
        return _submit(
            event, job_store=job_store, lambda_client=lambda_client, persistence=persistence
        )
    if _is_poll_request(event):
        return _poll(event, job_store=job_store, persistence=persistence)
    return _not_found()


def _submit(
    event: dict[str, Any],
    *,
    job_store: JobStore | None,
    lambda_client: Any | None,
    persistence: PersistenceConfig | None,
) -> dict[str, Any]:
    """Validate the body, create a ``queued`` record, async-invoke the worker, return 202."""
    try:
        request = _parse_request(event)
    except json.JSONDecodeError as exc:
        return _bad_request(
            _decode_error(f"invalid JSON: {exc.msg}", error_type="value_error.jsondecode")
        )
    except ValueError as exc:
        return _bad_request(_decode_error(str(exc), error_type="value_error"))
    except ValidationError as exc:
        return _bad_request(ValidationErrorResponse(detail=exc.errors(include_url=False)))

    persistence = persistence or PersistenceConfig.from_env()
    job_store = job_store or _default_job_store(persistence)
    lambda_client = lambda_client or _default_lambda_client(persistence)

    job_id = uuid4().hex
    try:
        job_store.create(job_id=job_id, query=request.query, max_turns=request.max_turns)
    except Exception as exc:  # noqa: BLE001 - cannot even persist the queued record
        logger.exception("failed to create queued job record")
        return _error_response(500, "submit_failed", str(exc))

    try:
        lambda_client.invoke(
            FunctionName=os.environ["WORKER_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps(
                {"job_id": job_id, "query": request.query, "max_turns": request.max_turns}
            ).encode(),
        )
    except Exception:  # noqa: BLE001 - mark failed but still return a pollable job id
        logger.error("failed to async-invoke worker for job %s", job_id, exc_info=True)
        job_store.mark_failed(job_id, WorkerError(message="could not start worker"))

    body = SubmitResponse(job_id=job_id, poll_url=f"/jobs/{job_id}")
    return {
        "statusCode": 202,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body.model_dump(mode="json")),
    }


def _poll(
    event: dict[str, Any],
    *,
    job_store: JobStore | None,
    persistence: PersistenceConfig | None,
) -> dict[str, Any]:
    """Read a job by id: ``404`` when missing/empty, ``200`` when found, ``500`` on a read error."""
    job_id = _job_id_from_event(event)
    if not job_id:
        return _not_found()

    persistence = persistence or PersistenceConfig.from_env()
    job_store = job_store or _default_job_store(persistence)

    try:
        job = job_store.get(job_id)
    except Exception as exc:  # noqa: BLE001 - a DynamoDB read failure is a transient 500
        logger.error("failed to read job %s", job_id, exc_info=True)
        return _error_response(500, "poll_failed", str(exc))

    if job is None:
        return _not_found()
    return {
        "statusCode": 200,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(job.model_dump(mode="json", exclude_none=True)),
    }
