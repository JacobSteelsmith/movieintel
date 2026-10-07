"""Agent orchestration-loop tests with mocked Bedrock (Task 9, design §3.7).

All Bedrock Converse responses are scripted via :class:`CapturingConverseClient`; the loop
dispatches into the real Task 8 tools against the ``StubRepository`` / ``FakeAgentRuntime``
hand stubs. These tests assert the control flow (tool dispatch, toolResult ordering,
MAX_TURNS bound, guardrail refusal, validation surfacing) — not model intelligence, which
the scripted fake stands in for.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.loop import (
    MAX_TURNS,
    build_agent_system_prompt,
    is_guardrail_intervention,
    run_agent,
)
from movieintel.agent.response import (
    AgentResult,
    BoundedResponse,
    ComparativeAnalysis,
    PreferenceSummary,
    RecommendationList,
    RefusalResponse,
)
from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
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

# --------------------------------------------------------------------------- helpers


def _recommendations_payload() -> dict[str, Any]:
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


def _preferences_payload() -> dict[str, Any]:
    return {
        "kind": "preferences",
        "subject": "user-7",
        "summary": "Prefers uplifting, high-PES dramas.",
        "highlights": ["Likes positive sentiment", "Favors high revenue"],
    }


def _comparison_payload() -> dict[str, Any]:
    return {
        "kind": "comparison",
        "subjects": ["1", "2"],
        "dimensions": ["revenue", "pes"],
        "narrative": "Film 1 leads on PES; film 2 leads on revenue.",
    }


def _run(
    client: CapturingConverseClient,
    *,
    repository: StubRepository,
    kb_client: FakeAgentRuntime,
    config: BedrockConfig,
    kb_config: KBConfig,
    user_request: str = "Recommend action movies.",
    max_turns: int = MAX_TURNS,
) -> AgentResult:
    return run_agent(
        user_request,
        client=client,
        repository=repository,
        kb_client=kb_client,
        kb_config=kb_config,
        config=config,
        max_turns=max_turns,
    )


# ----------------------------------------------------- 1. single tool-use round-trip


def test_single_tool_use_round_trip_returns_recommendation_list(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block(
                    "query_movies",
                    {"sentiment": "positive", "genres": ["action"]},
                    tool_use_id="t1",
                )
            ),
            converse_final(_recommendations_payload()),
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RecommendationList)
    assert result.kind == "recommendations"
    # The model's input was dispatched into the real tool (sentiment -> GSI query path).
    assert [name for name, _ in repo.calls] == ["query_by_sentiment"]
    assert repo.calls[0][1]["sentiment"] == "positive"


# ------------------------------------------------------- 2. multi-tool / multi-turn


def test_multi_tool_multi_turn_appends_tool_results_in_order(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(batch=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("semantic_search", {"query": "space epics"}, tool_use_id="t1")
            ),
            converse_tool_use(
                tool_use_block("compare_movies", {"movie_ids": ["1", "2"]}, tool_use_id="t2")
            ),
            converse_final(_comparison_payload()),
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, ComparativeAnalysis)
    # KB and repo were each hit once, in turn.
    assert len(kb.calls) == 1
    assert [name for name, _ in repo.calls] == ["batch_get"]

    # The toolResult blocks are appended in order across turns: the request on turn 2
    # carries the semantic_search result; the request on turn 3 carries the compare result.
    turn2_messages = client.requests[1]["messages"]
    turn3_messages = client.requests[2]["messages"]
    assert _last_tool_result_id(turn2_messages) == "t1"
    assert _last_tool_result_id(turn3_messages) == "t2"


def _last_tool_result_id(messages: list[dict[str, Any]]) -> str:
    """The toolUseId of the toolResult in the final user turn of ``messages``."""
    last_user = [m for m in messages if m["role"] == "user"][-1]
    for block in last_user["content"]:
        if "toolResult" in block:
            return str(block["toolResult"]["toolUseId"])
    raise AssertionError("no toolResult block found in the final user turn")


# ---------------------------------------------------------- 3. MAX_TURNS termination


def test_max_turns_termination_returns_bounded_without_raising(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    # The model requests a tool forever and never calls final_answer.
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id=f"t{i}")
            )
            for i in range(10)
        ]
    )

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        max_turns=3,
    )

    assert isinstance(result, BoundedResponse)
    assert result.turns_used == 3
    assert len(client.requests) == 3  # loop stopped exactly at the bound


def test_default_max_turns_is_six() -> None:
    assert MAX_TURNS == 6


# ----------------------------------------- 4. guardrail intervention via stopReason


def test_guardrail_via_stop_reason_returns_refusal_no_dispatch(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([converse_guardrail()])

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RefusalResponse)
    assert result.reason
    assert repo.calls == []  # no tool dispatch
    assert kb.calls == []  # no KB call / no content read
    assert len(client.requests) == 1


# ------------------------------- 5. guardrail intervention via trace, other stopReason


def test_guardrail_via_trace_with_different_stop_reason_returns_refusal(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    # stopReason is NOT the guardrail literal, but a guardrail trace block is present.
    client = CapturingConverseClient([converse_guardrail(stop_reason="end_turn")])

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RefusalResponse)
    assert repo.calls == []
    assert kb.calls == []


def test_is_guardrail_intervention_signals() -> None:
    """The centralized detector catches more than the single stopReason value."""
    assert is_guardrail_intervention({"stopReason": "guardrail_intervened"})
    assert is_guardrail_intervention(
        {"stopReason": "end_turn", "trace": {"guardrail": {"outputAssessments": {}}}}
    )
    assert is_guardrail_intervention({"action": "GUARDRAIL_INTERVENED"})
    assert is_guardrail_intervention({"guardrailAction": "INTERVENED"})
    assert is_guardrail_intervention({"amazon-bedrock-guardrailAction": "INTERVENED"})
    # A clean response is not an intervention.
    assert not is_guardrail_intervention({"stopReason": "end_turn"})
    assert not is_guardrail_intervention({"stopReason": "tool_use", "trace": {}})


# ------------------------------------------------ 6. invalid tool args -> error result


def test_invalid_tool_args_surfaced_as_error_tool_result(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            # sentiment not in the enum -> ToolValidationError from the Task 8 tool.
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "ecstatic"}, tool_use_id="t1")
            ),
            converse_final(_recommendations_payload()),
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RecommendationList)
    # The loop did not crash and did not touch the repository for the invalid call.
    assert repo.calls == []
    # Turn 2 request carries an error toolResult with the serialized validation detail.
    turn2_messages = client.requests[1]["messages"]
    block = _last_tool_result_block(turn2_messages)
    assert block["status"] == "error"
    payload = block["content"][0]["json"]
    assert payload["tool"] == "query_movies"
    assert payload["error"] == "invalid_arguments"
    assert payload["detail"]


def _last_tool_result_block(messages: list[dict[str, Any]]) -> dict[str, Any]:
    last_user = [m for m in messages if m["role"] == "user"][-1]
    for block in last_user["content"]:
        if "toolResult" in block:
            return dict(block["toolResult"])
    raise AssertionError("no toolResult block found in the final user turn")


# -------------------------------------------------------- 7. unknown tool name


def test_unknown_tool_name_returns_error_result_no_crash(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(tool_use_block("recommend_please", {"x": 1}, tool_use_id="t1")),
            converse_final(_recommendations_payload()),
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RecommendationList)
    assert repo.calls == []  # unknown tool never touched the repo
    assert kb.calls == []
    block = _last_tool_result_block(client.requests[1]["messages"])
    assert block["status"] == "error"
    payload = block["content"][0]["json"]
    assert payload["tool"] == "recommend_please"
    assert payload["error"] == "unknown_tool"


# ----------------------------- 8. guardrailConfig + modelId + toolConfig on every call


def test_guardrail_and_model_id_and_tool_config_on_every_request(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(
        by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)],
        batch=[make_enriched(movie_id=1, pes_value=90.0)],
    )
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_tool_use(
                tool_use_block("compare_movies", {"movie_ids": ["1", "2"]}, tool_use_id="t2")
            ),
            converse_final(_recommendations_payload()),
        ]
    )

    _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert len(client.requests) == 3
    for request in client.requests:
        assert request["guardrailConfig"] == {
            "guardrailIdentifier": "gr-test",
            "guardrailVersion": "DRAFT",
        }
        assert request["modelId"] == config.model_id == "anthropic.claude-test-v1:0"
        tool_names = {spec["toolSpec"]["name"] for spec in request["toolConfig"]["tools"]}
        assert {"query_movies", "semantic_search", "compare_movies", "final_answer"} <= tool_names


# ---------------------------------------------------------- 9. example-prompt routing


def test_example_prompt_recommend_routes_to_query_movies(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block(
                    "query_movies",
                    {"sentiment": "positive", "genres": ["action"], "min_revenue": 1_000_000},
                    tool_use_id="t1",
                )
            ),
            converse_final(_recommendations_payload()),
        ]
    )

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        user_request="Recommend action movies with high revenue and positive sentiment.",
    )

    assert isinstance(result, RecommendationList)
    assert [name for name, _ in repo.calls] == ["query_by_sentiment"]


def test_example_prompt_summarize_preferences_routes_and_returns_summary(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(_preferences_payload()),
        ]
    )

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        user_request="Summarize preferences for a user based on their ratings.",
    )

    assert isinstance(result, PreferenceSummary)
    assert [name for name, _ in repo.calls] == ["query_by_sentiment"]


# ---------------------------------------------------- 10. final_answer validation guard


def test_invalid_final_answer_surfaced_then_valid_returns_result(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    # First final_answer is missing the required query_summary -> validation error.
    bad_payload = {"kind": "recommendations", "movies": []}
    client = CapturingConverseClient(
        [
            converse_final(bad_payload, tool_use_id="f1"),
            converse_final(_recommendations_payload(), tool_use_id="f2"),
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, RecommendationList)
    # The invalid final_answer was surfaced as an error toolResult (model can correct),
    # not returned as an invalid object.
    block = _last_tool_result_block(client.requests[1]["messages"])
    assert block["toolUseId"] == "f1"
    assert block["status"] == "error"
    assert len(client.requests) == 2


def test_non_tool_use_stop_without_final_answer_returns_bounded(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    # Model stops with prose and no final_answer tool call.
    client = CapturingConverseClient(
        [
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "here you go"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        ]
    )

    result = _run(client, repository=repo, kb_client=kb, config=config, kb_config=kb_config)

    assert isinstance(result, BoundedResponse)
    assert result.turns_used == 1


# ------------------------------------------------- 11. system-prompt graceful termination


def test_system_prompt_instructs_graceful_termination() -> None:
    """The system prompt guides a small attempt bound and finalizing on sparse results.

    Substring checks are intentionally loose (case-insensitive) so the wording can evolve;
    they only pin the behavioral signals Fix A adds and confirm the tools are still named.
    """
    prompt = build_agent_system_prompt().lower()

    # (i) a small tool-search attempt bound.
    assert "two" in prompt and ("attempt" in prompt or "search" in prompt)
    # (ii) call final_answer even when results are few/empty.
    assert "final_answer" in prompt
    assert "few" in prompt or "empty" in prompt
    # (iii) do not repeat near-identical searches.
    assert "repeat" in prompt or "near-identical" in prompt
    # The three tools plus final_answer are still named.
    for tool in ("query_movies", "semantic_search", "compare_movies", "final_answer"):
        assert tool in prompt
