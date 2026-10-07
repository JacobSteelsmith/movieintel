"""Shared fixtures for the agent-loop tests (Task 9, design §3.7).

All Bedrock Converse calls are mocked by :class:`CapturingConverseClient`, a scripted,
request-capturing fake that mirrors the ``_CapturingClient`` pattern from
``tests/enrichment/test_enrich_movie.py``. The ``converse_tool_use`` /
``converse_final`` / ``converse_guardrail`` builders assemble realistic Converse response
envelopes (parallel to ``converse_response`` in the enrichment tests). The repository and
KB stubs are reused from ``tests/agent/tools/conftest.py`` so the loop dispatches into the
real Task 8 tools against hand stubs that record every call.

NO real Bedrock, no ``@pytest.mark.live`` test is needed.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig

# Re-export the Task 8 stubs so the loop tests can build repository/KB dependencies.
from tests.agent.tools.conftest import (
    FakeAgentRuntime,
    StubRepository,
    make_enriched,
)

__all__ = [
    "CapturingConverseClient",
    "FakeAgentRuntime",
    "StubRepository",
    "config",
    "converse_final",
    "converse_guardrail",
    "converse_tool_use",
    "kb_config",
    "make_enriched",
    "tool_use_block",
]


@pytest.fixture
def config() -> BedrockConfig:
    """A fully configured BedrockConfig (model id + Guardrail) for the loop."""
    return BedrockConfig.from_env(
        env={
            "BEDROCK_MODEL_ID": "anthropic.claude-test-v1:0",
            "BEDROCK_GUARDRAIL_ID": "gr-test",
            "BEDROCK_GUARDRAIL_VERSION": "DRAFT",
        }
    )


@pytest.fixture
def kb_config() -> KBConfig:
    """A configured KBConfig so the semantic_search tool can build a Retrieve request."""
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "kb-test"})


def converse_tool_use(
    *blocks: dict[str, Any],
    stop_reason: str = "tool_use",
) -> dict[str, Any]:
    """Build a Converse response whose assistant message carries ``toolUse`` blocks.

    Each entry in ``blocks`` is a ``{"toolUseId", "name", "input"}`` dict; it is wrapped in
    a ``{"toolUse": ...}`` content block in order.
    """
    content = [{"toolUse": block} for block in blocks]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30},
        "metrics": {"latencyMs": 100},
    }


def tool_use_block(name: str, tool_input: dict[str, Any], *, tool_use_id: str) -> dict[str, Any]:
    """Construct a single ``toolUse`` block payload for :func:`converse_tool_use`."""
    return {"toolUseId": tool_use_id, "name": name, "input": tool_input}


def converse_final(payload: dict[str, Any], *, tool_use_id: str = "final-1") -> dict[str, Any]:
    """Build a Converse response where the model calls the ``final_answer`` tool."""
    return converse_tool_use(tool_use_block("final_answer", payload, tool_use_id=tool_use_id))


def converse_guardrail(stop_reason: str = "guardrail_intervened") -> dict[str, Any]:
    """Build a guardrail-intervention Converse response (stopReason + trace block)."""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": ""}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 10, "outputTokens": 0, "totalTokens": 10},
        "metrics": {"latencyMs": 50},
        "trace": {"guardrail": {"outputAssessments": {}}},
    }


class CapturingConverseClient:
    """A scripted Converse client that records every request it receives.

    Scripts the multi-turn Converse exchange: each call pops the next response. A call
    beyond the script raises, so tests that assert a bounded call count fail loudly if the
    loop over-calls.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        # Deep-copy so each captured request snapshots the message list as it was at call
        # time; the loop mutates its ``messages`` list in place across turns.
        self.requests.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("converse called more times than scripted")
        return self._responses.pop(0)
