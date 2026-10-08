"""OpenAPI generation tests (Task 10 FEAT-001, REQ-B-4.2).

``docs/openapi.yaml`` is a PUBLISHED artifact generated from the Pydantic serving models
(``QueryRequest``, the ``AgentResult`` member models, ``ValidationErrorResponse``). These
tests assert the generated document documents POST ``/query`` with 200 and 400 responses
and that the on-disk file is in sync with the generator (regenerate it when the models
change). The spec is emitted as JSON-compatible YAML and parsed with ``json`` so no new
YAML dependency is introduced.
"""

from __future__ import annotations

import json
from pathlib import Path

from movieintel.serving.openapi import (
    OPENAPI_PATH,
    build_openapi,
    render_openapi_yaml,
)


def test_build_openapi_documents_query_path() -> None:
    spec = build_openapi()
    assert spec["openapi"].startswith("3.1")
    assert "/query" in spec["paths"]
    post = spec["paths"]["/query"]["post"]
    # Request body references the QueryRequest schema (query + max_turns).
    request_schema = post["requestBody"]["content"]["application/json"]["schema"]
    ref = request_schema.get("$ref", "")
    assert ref.endswith("/QueryRequest")
    query_request = spec["components"]["schemas"]["QueryRequest"]
    assert "query" in query_request["properties"]
    assert "max_turns" in query_request["properties"]
    assert query_request["required"] == ["query"]


def test_build_openapi_documents_200_and_400_responses() -> None:
    spec = build_openapi()
    responses = spec["paths"]["/query"]["post"]["responses"]
    assert "200" in responses
    assert "400" in responses
    # 200 is the AgentResult union (oneOf by 'kind').
    success_schema = responses["200"]["content"]["application/json"]["schema"]
    assert "oneOf" in success_schema
    assert len(success_schema["oneOf"]) == 5
    # 400 is the ValidationErrorResponse.
    error_schema = responses["400"]["content"]["application/json"]["schema"]
    assert error_schema["$ref"].endswith("/ValidationErrorResponse")
    assert "ValidationErrorResponse" in spec["components"]["schemas"]


def test_agent_result_member_schemas_are_present() -> None:
    spec = build_openapi()
    schemas = spec["components"]["schemas"]
    for name in (
        "RecommendationList",
        "PreferenceSummary",
        "ComparativeAnalysis",
        "BoundedResponse",
        "RefusalResponse",
    ):
        assert name in schemas, name


def test_build_openapi_documents_submit_jobs_path() -> None:
    spec = build_openapi()
    assert "/jobs" in spec["paths"]
    post = spec["paths"]["/jobs"]["post"]
    # Reuses the QueryRequest body.
    request_schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/QueryRequest")
    responses = post["responses"]
    assert "202" in responses
    assert "400" in responses
    accepted_schema = responses["202"]["content"]["application/json"]["schema"]
    assert accepted_schema["$ref"].endswith("/SubmitResponse")
    error_schema = responses["400"]["content"]["application/json"]["schema"]
    assert error_schema["$ref"].endswith("/ValidationErrorResponse")


def test_build_openapi_documents_poll_job_path() -> None:
    spec = build_openapi()
    assert "/jobs/{id}" in spec["paths"]
    get = spec["paths"]["/jobs/{id}"]["get"]
    # A required path parameter named 'id' of type string.
    params = get["parameters"]
    id_param = next(p for p in params if p["name"] == "id")
    assert id_param["in"] == "path"
    assert id_param["required"] is True
    assert id_param["schema"]["type"] == "string"
    responses = get["responses"]
    assert "200" in responses
    assert "404" in responses
    status_schema = responses["200"]["content"]["application/json"]["schema"]
    assert status_schema["$ref"].endswith("/JobStatusResponse")
    not_found_schema = responses["404"]["content"]["application/json"]["schema"]
    assert not_found_schema["$ref"].endswith("/NotFoundResponse")


def test_health_path_is_not_documented() -> None:
    # The generator intentionally omits /health.
    assert "/health" not in build_openapi()["paths"]


def test_job_component_schemas_are_present() -> None:
    spec = build_openapi()
    schemas = spec["components"]["schemas"]
    for name in (
        "SubmitResponse",
        "JobStatusResponse",
        "JobProgress",
        "WorkerError",
        "NotFoundResponse",
        # The enums come along as $defs via model_json_schema.
        "ProgressPhase",
        "JobStatus",
    ):
        assert name in schemas, name


def test_job_status_response_embeds_agent_result_union() -> None:
    spec = build_openapi()
    schemas = spec["components"]["schemas"]
    # JobStatusResponse.result references the AgentResult union schema.
    result_field = schemas["JobStatusResponse"]["properties"]["result"]
    refs = {sub.get("$ref", "") for sub in result_field["anyOf"]}
    assert any(ref.endswith("/AgentResult") for ref in refs)
    # The AgentResult schema is the five-member union over the result models.
    agent_result = schemas["AgentResult"]
    members = agent_result.get("oneOf") or agent_result.get("anyOf")
    assert members is not None
    assert len(members) == 5


def test_rendered_yaml_parses_as_json() -> None:
    rendered = render_openapi_yaml()
    # JSON-compatible YAML: parseable by the stdlib json module.
    parsed = json.loads(rendered)
    assert parsed == build_openapi()


def test_on_disk_openapi_is_in_sync() -> None:
    path = Path(OPENAPI_PATH)
    assert path.exists(), f"{path} must be generated and committed"
    on_disk = path.read_text(encoding="utf-8")
    assert on_disk == render_openapi_yaml(), (
        "docs/openapi.yaml is out of sync with the generator; "
        "regenerate it from the Pydantic models"
    )
    # And it parses.
    assert json.loads(on_disk)["paths"]["/query"]["post"]
