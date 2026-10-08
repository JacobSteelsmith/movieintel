"""Shared serving-handler helpers (async 202 + poll path, design Decision 3).

The request-decoding/validation and lazy dependency-construction logic was originally
private to ``infra/handlers/serve/handler.py``. It is factored here so the ``serve``,
``submit``, and ``worker`` handlers share ONE validation + dependency-building
implementation (one place the 400/decode invariants live). The ``serve`` handler imports
these and re-binds the ``_default_*``/``_parse_request`` names into its own module namespace
so its existing tests (which monkeypatch those names on the serve module) keep working.

Dependencies default to lazily-imported boto3-backed objects so the module imports cleanly
without AWS credentials; tests inject fakes and never reach the ``_default_*`` builders.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from movieintel.agent.tools import MovieRepository
from movieintel.config import BedrockConfig
from movieintel.enrichment.client import BedrockConverseClient
from movieintel.kb.client import BedrockAgentRuntimeClient
from movieintel.kb.config import KBConfig
from movieintel.persistence.config import PersistenceConfig
from movieintel.serving.schemas import QueryRequest, ValidationErrorResponse

_JSON_HEADERS = {"content-type": "application/json"}


def _default_bedrock_client(config: BedrockConfig) -> BedrockConverseClient:
    """Build the live ``bedrock-runtime`` client from the configured region.

    Imported lazily so the module imports cleanly without AWS creds (tests inject a client
    and never reach this path).
    """
    import boto3

    client: BedrockConverseClient = boto3.client("bedrock-runtime", region_name=config.region)
    return client


def _default_kb_client(kb_config: KBConfig) -> BedrockAgentRuntimeClient:
    """Build the live ``bedrock-agent-runtime`` client (KB Retrieve) from the KB region."""
    import boto3

    client: BedrockAgentRuntimeClient = boto3.client(
        "bedrock-agent-runtime", region_name=kb_config.region
    )
    return client


def _default_repository(persistence: PersistenceConfig) -> MovieRepository:
    """Build the live repository over the configured DynamoDB table."""
    import boto3

    from movieintel.persistence.repository import MovieIntelRepository

    table = boto3.resource("dynamodb", region_name=persistence.region).Table(persistence.table_name)
    return MovieIntelRepository(table=table, config=persistence)


def _decode_body(event: dict[str, Any]) -> str:
    """Return the request body text, decoding base64 when ``isBase64Encoded`` is set.

    :raises ValueError: when the body is missing/None or base64 decoding fails - the caller
        turns this into a structured 400 without invoking the agent.
    """
    body = event.get("body")
    if body is None:
        raise ValueError("request body is missing")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise ValueError("request body is not valid base64") from exc
    if not isinstance(body, str):
        raise ValueError("request body must be a string")
    return body


def _bad_request(error: ValidationErrorResponse) -> dict[str, Any]:
    """Build the structured 400 API-Gateway-proxy response (REQ-B-4.4)."""
    return {
        "statusCode": 400,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(error.model_dump(mode="json")),
    }


def _decode_error(message: str, *, error_type: str) -> ValidationErrorResponse:
    """Build a :class:`ValidationErrorResponse` for an undecodable/non-JSON body."""
    return ValidationErrorResponse(detail=[{"loc": ["body"], "msg": message, "type": error_type}])


def _parse_request(event: dict[str, Any]) -> QueryRequest:
    """Decode + validate the request body into a :class:`QueryRequest`.

    :raises ValidationError: on a schema-violating body.
    :raises ValueError / json.JSONDecodeError: on a missing/undecodable/non-JSON body.
    """
    raw = _decode_body(event)
    payload = json.loads(raw)
    return QueryRequest.model_validate(payload)
