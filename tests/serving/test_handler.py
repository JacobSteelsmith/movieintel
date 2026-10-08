"""Serving Lambda handler tests (Task 10 FEAT-001, REQ-B-4.1/.4, REQ-X-3.3).

The handler is a thin wrapper over :func:`movieintel.agent.loop.run_agent`: it validates the
API-Gateway-proxy request body against :class:`QueryRequest`, builds (or accepts injected)
deps, calls the loop, and serializes the ``AgentResult`` as an HTTP 200. A schema-violating
body returns a structured 400 and NEVER invokes the agent (REQ-B-4.4). A guardrail
intervention is a valid 200 structured ``RefusalResponse``.

All deps are injected: the scripted :class:`CapturingConverseClient` stands in for Bedrock,
``StubRepository`` for the repository, and ``FakeAgentRuntime`` for the KB client, so no
real boto3 client is constructed under test.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from handlers.serve.handler import handler

from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
from movieintel.persistence.config import PersistenceConfig
from tests.serving.conftest import (
    CapturingConverseClient,
    FakeAgentRuntime,
    StubRepository,
    converse_final,
    converse_guardrail,
    converse_tool_use,
    make_enriched,
    tool_use_block,
)

# --------------------------------------------------------------------------- helpers


def _proxy_event(body: str | None, *, is_base64: bool = False) -> dict[str, Any]:
    """Build a minimal API-Gateway-proxy event carrying ``body``."""
    return {
        "resource": "/query",
        "path": "/query",
        "httpMethod": "POST",
        "headers": {"content-type": "application/json"},
        "body": body,
        "isBase64Encoded": is_base64,
    }


def _health_event() -> dict[str, Any]:
    """Build a minimal API-Gateway-proxy GET /health event (no body)."""
    return {
        "routeKey": "GET /health",
        "rawPath": "/health",
        "requestContext": {"http": {"method": "GET", "path": "/health"}},
        "headers": {},
    }


def _call(
    event: dict[str, Any],
    *,
    client: CapturingConverseClient,
    repository: StubRepository,
    kb_client: FakeAgentRuntime,
    config: BedrockConfig,
    persistence: PersistenceConfig,
    kb_config: KBConfig,
) -> dict[str, Any]:
    return handler(
        event,
        None,
        client=client,
        repository=repository,
        kb_client=kb_client,
        config=config,
        persistence=persistence,
        kb_config=kb_config,
    )


# ------------------------------------------------- (a) valid request -> 200 AgentResult


def test_valid_request_returns_200_serialized_agent_result(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
    recommendations_payload: dict[str, Any],
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(recommendations_payload),
        ]
    )
    event = _proxy_event(json.dumps({"query": "Recommend action movies."}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    body = json.loads(response["body"])
    assert body["kind"] == "recommendations"
    assert body["movies"][0]["title"] == "A Film"
    # The agent WAS invoked for a valid request.
    assert len(client.requests) == 2


def test_valid_request_with_max_turns_is_honored(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    # The model never calls final_answer; a max_turns=2 override bounds the loop at 2.
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id=f"t{i}")
            )
            for i in range(5)
        ]
    )
    event = _proxy_event(json.dumps({"query": "loop please", "max_turns": 2}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["kind"] == "bounded"
    assert body["turns_used"] == 2
    assert len(client.requests) == 2  # bounded exactly at the override


def test_base64_encoded_body_is_decoded(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
    comparison_payload: dict[str, Any],
) -> None:
    repo = StubRepository(batch=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("compare_movies", {"movie_ids": ["1", "2"]}, tool_use_id="t1")
            ),
            converse_final(comparison_payload),
        ]
    )
    raw = json.dumps({"query": "Compare two films."}).encode("utf-8")
    event = _proxy_event(base64.b64encode(raw).decode("ascii"), is_base64=True)

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["kind"] == "comparison"


# ------------------------------------------------- (a2) GET /health warmup short-circuit


def test_health_request_returns_200_ok_without_invoking_agent(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])  # any converse call would raise

    response = _call(
        _health_event(),
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "ok"}
    # The warmup short-circuit never reaches the agent, repository, or KB client.
    assert client.requests == []
    assert repo.calls == []
    assert kb.calls == []


def test_bodyless_get_health_event_returns_200_not_400(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])
    # A GET /health event has no body; the short-circuit must answer 200 before the
    # missing-body path can produce a 400.
    event = _health_event()
    assert "body" not in event

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "ok"}
    assert client.requests == []


# --------------------------------------- (b) schema-violating body -> 400, no agent call


def test_missing_query_returns_400_without_invoking_agent(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])  # any converse call would raise
    event = _proxy_event(json.dumps({"not_query": "x"}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 400
    assert response["headers"]["content-type"] == "application/json"
    body = json.loads(response["body"])
    assert body["error"] == "invalid_request"
    assert body["detail"]
    # The agent was NEVER invoked (REQ-B-4.4).
    assert client.requests == []
    assert repo.calls == []
    assert kb.calls == []


def test_malformed_json_body_returns_400_without_invoking_agent(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])
    event = _proxy_event("{not valid json")

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"] == "invalid_request"
    assert body["detail"]
    assert client.requests == []


def test_extra_field_body_returns_400_without_invoking_agent(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])
    event = _proxy_event(json.dumps({"query": "ok", "unexpected": "x"}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"] == "invalid_request"
    assert client.requests == []


def test_missing_body_returns_400_without_invoking_agent(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([])
    event = _proxy_event(None)

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"] == "invalid_request"
    assert client.requests == []


# --------------------------------------- (c) guardrail intervention -> 200 RefusalResponse


def test_guardrail_intervention_returns_refusal_at_200(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([converse_guardrail()])
    event = _proxy_event(json.dumps({"query": "do something blocked"}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["kind"] == "refusal"
    assert body["reason"]
    # No tool dispatch / no content read on an intervention.
    assert repo.calls == []
    assert kb.calls == []


# ------------------------- (d) deps injected; no real boto3 client built under test


def test_handler_uses_injected_deps_and_builds_no_boto3(
    bedrock_config: BedrockConfig,
    persistence_config: PersistenceConfig,
    kb_config: KBConfig,
    monkeypatch: Any,
    recommendations_payload: dict[str, Any],
) -> None:
    # If the handler tried to build a real client, importing boto3 would be fine, but any
    # attribute access on a sentinel would blow up; we assert instead that the injected
    # client received the calls and the module-level default builder is never reached.
    import handlers.serve.handler as serve_module

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handler must not build a real client when deps are injected")

    monkeypatch.setattr(serve_module, "_default_bedrock_client", _boom)
    monkeypatch.setattr(serve_module, "_default_kb_client", _boom)
    monkeypatch.setattr(serve_module, "_default_repository", _boom)

    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(recommendations_payload),
        ]
    )
    event = _proxy_event(json.dumps({"query": "Recommend action movies."}))

    response = _call(
        event,
        client=client,
        repository=repo,
        kb_client=kb,
        config=bedrock_config,
        persistence=persistence_config,
        kb_config=kb_config,
    )

    assert response["statusCode"] == 200
    assert len(client.requests) == 2
