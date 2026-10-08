"""Fixtures for serving-handler tests (Task 10 FEAT-001).

Reuses the existing test doubles rather than inventing new ones:

- :class:`CapturingConverseClient` + the ``converse_tool_use`` / ``converse_final`` /
  ``converse_guardrail`` / ``tool_use_block`` builders from ``tests/agent/conftest.py``
  script the Bedrock Converse exchange so ``run_agent`` runs with no real Bedrock.
- :class:`StubRepository` / :class:`FakeAgentRuntime` / ``make_enriched`` and the
  moto-backed ``repository`` fixture come from ``tests/agent/tools/conftest.py``.

Plus configured ``BedrockConfig`` (model id + guardrail), ``PersistenceConfig``, and
``KBConfig`` fixtures so the handler can be driven with fully-injected deps and never
constructs a real boto3 client under test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.repository import MovieIntelRepository
from movieintel.serving.jobs_store import JobStore

# Re-export the scripted Converse fake + builders and the repo/KB stubs so serving tests
# import them from one place (mirrors how ``tests/agent/conftest.py`` re-exports them).
from tests.agent.conftest import (
    CapturingConverseClient,
    FakeAgentRuntime,
    StubRepository,
    converse_final,
    converse_guardrail,
    converse_tool_use,
    make_enriched,
    tool_use_block,
)

__all__ = [
    "CapturingConverseClient",
    "FakeAgentRuntime",
    "FakeLambdaClient",
    "StubRepository",
    "bedrock_config",
    "converse_final",
    "converse_guardrail",
    "converse_tool_use",
    "kb_config",
    "make_enriched",
    "moto_job_store",
    "moto_repository",
    "persistence_config",
    "tool_use_block",
]


class FakeLambdaClient:
    """A boto3 ``lambda`` client double recording each ``invoke`` without dispatching.

    Each call captures the ``FunctionName``, ``InvocationType``, and the DECODED JSON
    ``Payload`` so the submit test can assert exactly one ``Event`` invoke with the right
    ``job_id``/``query``/``max_turns``. Set ``raise_on_invoke=True`` to simulate an
    async-invoke failure (the submit handler then marks the job ``failed`` and still 202s).
    """

    def __init__(self, *, raise_on_invoke: bool = False) -> None:
        self.raise_on_invoke = raise_on_invoke
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        payload: Any = kwargs.get("Payload")
        decoded = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        self.invocations.append(
            {
                "FunctionName": kwargs.get("FunctionName"),
                "InvocationType": kwargs.get("InvocationType"),
                "Payload": decoded,
            }
        )
        if self.raise_on_invoke:
            raise RuntimeError("simulated async invoke failure")
        return {"StatusCode": 202}


@pytest.fixture
def bedrock_config() -> BedrockConfig:
    """A fully configured BedrockConfig (model id + guardrail) for the handler."""
    return BedrockConfig.from_env(
        env={
            "BEDROCK_MODEL_ID": "anthropic.claude-test-v1:0",
            "BEDROCK_GUARDRAIL_ID": "gr-test",
            "BEDROCK_GUARDRAIL_VERSION": "DRAFT",
        }
    )


@pytest.fixture
def persistence_config() -> PersistenceConfig:
    """Default persistence config (table ``MovieIntel``, GSI ``GSI1``)."""
    return PersistenceConfig.from_env(env={})


@pytest.fixture
def kb_config() -> KBConfig:
    """A configured KBConfig so the semantic_search tool can build a Retrieve request."""
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "kb-test"})


@pytest.fixture
def moto_repository(persistence_config: PersistenceConfig) -> Iterator[MovieIntelRepository]:
    """A real repository wrapping a moto ``MovieIntel`` table (production class, mocked backend)."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=persistence_config.region)
        table = resource.create_table(
            TableName=persistence_config.table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": persistence_config.gsi1_name,
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield MovieIntelRepository(table=table, config=persistence_config)


@pytest.fixture
def moto_job_store(persistence_config: PersistenceConfig) -> Iterator[JobStore]:
    """A :class:`JobStore` over a moto ``MovieIntel`` table (same schema as ``moto_repository``)."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=persistence_config.region)
        table = resource.create_table(
            TableName=persistence_config.table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": persistence_config.gsi1_name,
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield JobStore(table=table, config=persistence_config)


def _recommendations_payload() -> dict[str, Any]:
    """A valid ``final_answer`` recommendations payload (mirrors tests/agent/test_loop)."""
    return {
        "kind": "recommendations",
        "query_summary": "Action movies with high revenue and positive sentiment.",
        "movies": [
            {
                "movie_id": "1",
                "title": "A Film",
                "sentiment": "positive",
                "pes": 90.0,
                "mood": "uplifting",
                "rationale": "High PES and positive sentiment.",
            }
        ],
    }


def _comparison_payload() -> dict[str, Any]:
    """A valid ``final_answer`` comparison payload."""
    return {
        "kind": "comparison",
        "subjects": ["1", "2"],
        "dimensions": ["revenue", "pes"],
        "narrative": "Film 1 leads on PES; film 2 leads on revenue.",
    }


@pytest.fixture
def recommendations_payload() -> dict[str, Any]:
    return _recommendations_payload()


@pytest.fixture
def comparison_payload() -> dict[str, Any]:
    return _comparison_payload()
