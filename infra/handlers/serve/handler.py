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

import json
from typing import Any

from pydantic import ValidationError

# The validation/decoding/dependency helpers now live in ``handlers.shared.serving`` and are
# re-bound here (as module-level names) so this handler's existing tests, which monkeypatch
# ``serve_module._default_bedrock_client``/``_default_kb_client``/``_default_repository``,
# still patch the exact names this module calls. The serve handler's public behavior is
# unchanged.
from handlers.shared.serving import (
    _JSON_HEADERS,
    _bad_request,
    _decode_error,
    _default_bedrock_client,
    _default_kb_client,
    _default_repository,
    _parse_request,
)
from movieintel.agent.loop import MAX_TURNS, run_agent
from movieintel.agent.tools import MovieRepository
from movieintel.config import BedrockConfig
from movieintel.enrichment.client import BedrockConverseClient
from movieintel.kb.client import BedrockAgentRuntimeClient
from movieintel.kb.config import KBConfig
from movieintel.persistence.config import PersistenceConfig
from movieintel.serving.schemas import ValidationErrorResponse

__all__ = ["handler"]

#: GET /health is the warmup short-circuit route (design 2.3). It returns immediately with
#: a tiny JSON body and never touches the agent loop, config, clients, or repository.
_HEALTH_PATH = "/health"
_HEALTH_METHOD = "GET"


def _is_health_request(event: dict[str, Any]) -> bool:
    """True when the event is a GET /health request across the proxy shapes we accept.

    Matches the API-Gateway HTTP-API ``routeKey`` (``GET /health``), the v2
    ``requestContext.http.{method,path}`` pair, or the v1-style top-level
    ``httpMethod`` + (``rawPath`` or ``path``).
    """
    if event.get("routeKey") == f"{_HEALTH_METHOD} {_HEALTH_PATH}":
        return True
    http = event.get("requestContext", {}).get("http", {})
    if (http.get("method"), http.get("path")) == (_HEALTH_METHOD, _HEALTH_PATH):
        return True
    return event.get("httpMethod") == _HEALTH_METHOD and (
        event.get("rawPath") == _HEALTH_PATH or event.get("path") == _HEALTH_PATH
    )


def _health_response() -> dict[str, Any]:
    """Build the minimal GET /health 200 API-Gateway-proxy response."""
    return {
        "statusCode": 200,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps({"status": "ok"}),
    }


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
    # Warmup short-circuit (design 2.3): GET /health is intentionally minimal - it builds
    # no boto3 client, dependency, or config and never reaches the agent, because the
    # container init already loads the full import graph (so hitting /health warms the real
    # serving container) and warmup must add NO new IAM/Bedrock/DynamoDB surface.
    if _is_health_request(event):
        return _health_response()

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
