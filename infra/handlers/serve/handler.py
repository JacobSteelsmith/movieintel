"""Serving handler: HTTP POST /query over the Task 9 agent loop (Task 10, REQ-B-4).

Thin wrapper over :func:`movieintel.agent.loop.run_agent`; it reimplements no loop, tool,
repository, or config logic. The flow mirrors ``infra/handlers/enrich/handler.py``:

1. Decode the API-Gateway-proxy request body (honoring ``isBase64Encoded``).
2. Validate it against :class:`movieintel.serving.schemas.QueryRequest`. A malformed JSON
   body (``json.JSONDecodeError``) or a schema-violating body (``ValidationError``) returns
   a structured HTTP 400 (:class:`ValidationErrorResponse`) WITHOUT building any dependency
   or invoking the agent (REQ-B-4.4) - the agent is never reached for a bad request body.
3. For a valid body, build real dependencies from the environment when not injected
   (``BedrockConfig``/``PersistenceConfig``/``KBConfig`` own ALL ids and the model id - no
   inline resource/model literals here), call ``run_agent``, and return the serialized
   :class:`~movieintel.agent.response.AgentResult` as HTTP 200.

Status-code choice: 400 is returned ONLY for a schema-violating or undecodable request
body. Every modeled agent outcome - including a Guardrail refusal
(:class:`~movieintel.agent.response.RefusalResponse`) and the ``MAX_TURNS`` bound
(:class:`~movieintel.agent.response.BoundedResponse`) - is a valid structured result and is
returned at HTTP 200, because those are successful, policy-compliant responses to a
well-formed request, not request errors.

Dependencies (``client``/``repository``/``kb_client``/``config``/``persistence``/
``kb_config``) are injectable for tests and default to lazily-imported boto3-backed objects
on the live path, so the module imports cleanly without AWS credentials.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import ValidationError

from movieintel.agent.loop import MAX_TURNS, run_agent
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


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    client: BedrockConverseClient | None = None,
    repository: MovieRepository | None = None,
    kb_client: BedrockAgentRuntimeClient | None = None,
    config: BedrockConfig | None = None,
    persistence: PersistenceConfig | None = None,
    kb_config: KBConfig | None = None,
) -> dict[str, Any]:
    """Answer POST /query: validate the body, run the agent, serialize the result.

    Returns an API-Gateway-proxy response. A schema-violating or undecodable body yields a
    structured HTTP 400 (:class:`ValidationErrorResponse`) and never builds a dependency or
    invokes the agent (REQ-B-4.4). A valid body yields HTTP 200 whose body is the serialized
    :class:`~movieintel.agent.response.AgentResult`; a Guardrail refusal is one such valid
    200 outcome, not a request error.
    """
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

    config = config or BedrockConfig.from_env()
    persistence = persistence or PersistenceConfig.from_env()
    kb_config = kb_config or KBConfig.from_env()
    client = client or _default_bedrock_client(config)
    repository = repository or _default_repository(persistence)
    kb_client = kb_client or _default_kb_client(kb_config)

    result = run_agent(
        request.query,
        client=client,
        repository=repository,
        kb_client=kb_client,
        kb_config=kb_config,
        config=config,
        max_turns=request.max_turns or MAX_TURNS,
    )

    return {
        "statusCode": 200,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(result.model_dump(mode="json")),
    }
