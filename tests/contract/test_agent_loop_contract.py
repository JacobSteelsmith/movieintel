"""Contract test: the agent loop emits a well-formed Converse conversation (REQ-X-3.2).

Drives the full ``tool_use -> toolResult -> tool_use -> toolResult -> final_answer``
sequence end-to-end and asserts the message list the loop builds is valid Converse:
assistant ``toolUse`` turns alternate with user ``toolResult`` turns, and every
``toolResult`` references the ``toolUseId`` of the assistant ``toolUse`` that preceded it.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.loop import run_agent
from movieintel.agent.response import ComparativeAnalysis
from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
from tests.agent.conftest import (
    CapturingConverseClient,
    FakeAgentRuntime,
    StubRepository,
    converse_final,
    converse_tool_use,
    make_enriched,
    tool_use_block,
)


def _config() -> BedrockConfig:
    return BedrockConfig.from_env(
        env={"BEDROCK_MODEL_ID": "anthropic.claude-test-v1:0", "BEDROCK_GUARDRAIL_ID": "gr-test"}
    )


def _kb_config() -> KBConfig:
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "kb-test"})


def test_full_multi_turn_conversation_is_well_formed_converse() -> None:
    repo = StubRepository(
        by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)],
        batch=[make_enriched(movie_id=1, pes_value=90.0)],
    )
    kb = FakeAgentRuntime()
    comparison = {
        "kind": "comparison",
        "subjects": ["1", "2"],
        "dimensions": ["pes"],
        "narrative": "Film 1 leads on PES.",
    }
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_tool_use(
                tool_use_block("compare_movies", {"movie_ids": ["1", "2"]}, tool_use_id="t2")
            ),
            converse_final(comparison, tool_use_id="t3"),
        ]
    )

    result = run_agent(
        "Compare the top two movies.",
        client=client,
        repository=repo,
        kb_client=kb,
        kb_config=_kb_config(),
        config=_config(),
    )

    assert isinstance(result, ComparativeAnalysis)

    # The final request's message list is the full conversation the loop built.
    messages = client.requests[-1]["messages"]
    _assert_well_formed_converse(messages)

    # Expected alternation: user(request), assistant(toolUse t1), user(toolResult t1),
    # assistant(toolUse t2), user(toolResult t2).
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert _tool_use_ids(messages[1]) == ["t1"]
    assert _tool_result_ids(messages[2]) == ["t1"]
    assert _tool_use_ids(messages[3]) == ["t2"]
    assert _tool_result_ids(messages[4]) == ["t2"]


def _assert_well_formed_converse(messages: list[dict[str, Any]]) -> None:
    """Every assistant toolUse turn is immediately followed by a user toolResult turn with
    matching ids, and vice versa."""
    for i, message in enumerate(messages):
        use_ids = _tool_use_ids(message)
        if message["role"] == "assistant" and use_ids:
            nxt = messages[i + 1]
            assert nxt["role"] == "user"
            assert _tool_result_ids(nxt) == use_ids


def _tool_use_ids(message: dict[str, Any]) -> list[str]:
    return [b["toolUse"]["toolUseId"] for b in message["content"] if "toolUse" in b]


def _tool_result_ids(message: dict[str, Any]) -> list[str]:
    return [b["toolResult"]["toolUseId"] for b in message["content"] if "toolResult" in b]
