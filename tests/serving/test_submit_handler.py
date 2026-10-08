"""Submit + poll handler tests (async 202 + poll path, design Decision 1 & 3, AC-1..6).

Drives the submit/poll handler with an injected :class:`JobStore` (over a moto table) and a
:class:`FakeLambdaClient` so no real AWS is touched. Asserts the 202 body shape, that a
``queued`` record is created BEFORE exactly one ``Event`` invoke, 400 on a bad body (no
record, no invoke), the async-invoke-failure path (marks ``failed`` + still 202), the exact
route-dispatch rules, and the poll outcomes (found / 404 / 500 ``poll_failed``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from handlers.submit.handler import handler

from movieintel.serving.jobs import JobStatus
from movieintel.serving.jobs_store import JobStore
from tests.serving.conftest import FakeLambdaClient


@pytest.fixture(autouse=True)
def _worker_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "MovieIntelWorkerFn")


# --------------------------------------------------------------------------- events


def _submit_event(body: str | None) -> dict[str, Any]:
    return {
        "routeKey": "POST /jobs",
        "requestContext": {"http": {"method": "POST", "path": "/jobs"}},
        "headers": {"content-type": "application/json"},
        "body": body,
        "isBase64Encoded": False,
    }


def _poll_event_routekey(job_id: str) -> dict[str, Any]:
    return {
        "routeKey": "GET /jobs/{id}",
        "requestContext": {"http": {"method": "GET", "path": f"/jobs/{job_id}"}},
        "pathParameters": {"id": job_id},
    }


def _poll_event_resolved_path(job_id: str) -> dict[str, Any]:
    # No routeKey template and no pathParameters: id must come from the resolved path.
    return {
        "requestContext": {"http": {"method": "GET", "path": f"/jobs/{job_id}"}},
    }


# --------------------------------------------------------------- submit 202 + ordering


def test_submit_returns_202_body_shape(moto_job_store: JobStore) -> None:
    fake_lambda = FakeLambdaClient()
    event = _submit_event(json.dumps({"query": "recommend action movies"}))

    response = handler(event, None, job_store=moto_job_store, lambda_client=fake_lambda)

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert len(body["job_id"]) == 32
    assert int(body["job_id"], 16) >= 0  # 32-char lowercase hex
    assert body["status"] == "queued"
    assert body["poll_url"] == f"/jobs/{body['job_id']}"


def test_submit_creates_queued_record_before_single_event_invoke(moto_job_store: JobStore) -> None:
    fake_lambda = FakeLambdaClient()
    event = _submit_event(json.dumps({"query": "recommend action", "max_turns": 8}))

    response = handler(event, None, job_store=moto_job_store, lambda_client=fake_lambda)
    job_id = json.loads(response["body"])["job_id"]

    # The queued record exists (created BEFORE invoke).
    stored = moto_job_store.get(job_id)
    assert stored is not None
    assert stored.status == JobStatus.queued

    # Exactly one Event invoke with the matching payload.
    assert len(fake_lambda.invocations) == 1
    inv = fake_lambda.invocations[0]
    assert inv["FunctionName"] == "MovieIntelWorkerFn"
    assert inv["InvocationType"] == "Event"
    assert inv["Payload"] == {"job_id": job_id, "query": "recommend action", "max_turns": 8}


# ------------------------------------------------------------- 400 bad body, no effects


def test_submit_bad_body_returns_400_no_record_no_invoke(moto_job_store: JobStore) -> None:
    fake_lambda = FakeLambdaClient()
    event = _submit_event(json.dumps({"not_query": "x"}))

    response = handler(event, None, job_store=moto_job_store, lambda_client=fake_lambda)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "invalid_request"
    assert fake_lambda.invocations == []


# ------------------------------------------------------- async-invoke failure -> failed


def test_submit_invoke_failure_marks_failed_but_returns_202(moto_job_store: JobStore) -> None:
    fake_lambda = FakeLambdaClient(raise_on_invoke=True)
    event = _submit_event(json.dumps({"query": "recommend action"}))

    response = handler(event, None, job_store=moto_job_store, lambda_client=fake_lambda)

    assert response["statusCode"] == 202
    job_id = json.loads(response["body"])["job_id"]
    stored = moto_job_store.get(job_id)
    assert stored is not None
    assert stored.status == JobStatus.failed
    assert stored.error is not None
    assert stored.error.message == "could not start worker"


# ----------------------------------------------------------------- route dispatch rules


def test_poll_event_routekey_form_routes_to_poll(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="abc", query="q", max_turns=None)
    response = handler(_poll_event_routekey("abc"), None, job_store=moto_job_store)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["job_id"] == "abc"


def test_poll_event_resolved_path_form_routes_to_poll(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="abc", query="q", max_turns=None)
    response = handler(_poll_event_resolved_path("abc"), None, job_store=moto_job_store)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["job_id"] == "abc"


def test_unmatched_route_returns_404(moto_job_store: JobStore) -> None:
    event = {"requestContext": {"http": {"method": "DELETE", "path": "/other"}}}
    response = handler(event, None, job_store=moto_job_store)
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error"] == "not_found"


# --------------------------------------------------------------------- poll outcomes


def test_poll_returns_stored_record(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="abc", query="q", max_turns=None)
    moto_job_store.mark_running("abc")
    response = handler(_poll_event_routekey("abc"), None, job_store=moto_job_store)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "running"
    assert body["progress"]["phase"] == "understanding"
    # In-flight: result/error omitted via exclude_none.
    assert "result" not in body
    assert "error" not in body


def test_poll_unknown_id_returns_404(moto_job_store: JobStore) -> None:
    response = handler(_poll_event_routekey("nope"), None, job_store=moto_job_store)
    assert response["statusCode"] == 404


def test_poll_read_error_returns_500_poll_failed() -> None:
    class RaisingStore:
        def get(self, job_id: str) -> Any:
            raise RuntimeError("ddb read failed")

    response = handler(_poll_event_routekey("abc"), None, job_store=RaisingStore())  # type: ignore[arg-type]
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["error"] == "poll_failed"
