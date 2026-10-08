"""Progress-emission tests for ``run_agent``'s optional ``on_progress`` callback (FEAT-001).

These assert the real per-phase progress sequence (design Decision 4) by passing a
list-appending callback and comparing the emitted ``(phase, turn)`` pairs against the
verified loop points. The backward-compatibility proof (``run_agent`` without
``on_progress`` is byte-for-byte identical) lives in ``tests/agent/test_loop.py``, which
passes unchanged.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.loop import run_agent
from movieintel.agent.progress import PHASE_LABELS, ProgressEvent, ProgressPhase
from movieintel.agent.response import (
    AgentResult,
    BoundedResponse,
    ComparativeAnalysis,
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
    on_progress: Any,
    max_turns: int = 6,
) -> AgentResult:
    return run_agent(
        "Recommend action movies.",
        client=client,
        repository=repository,
        kb_client=kb_client,
        kb_config=kb_config,
        config=config,
        max_turns=max_turns,
        on_progress=on_progress,
    )


def _pairs(events: list[ProgressEvent]) -> list[tuple[ProgressPhase, int]]:
    """The ``(phase, turn)`` sequence, with each event's label verified in passing."""
    for event in events:
        assert event.label == PHASE_LABELS[event.phase]
    return [(event.phase, event.turn) for event in events]


# ----------------------------------------------------- 1. recommendation (one search)


def test_recommendation_emits_understanding_searching_composing(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(_recommendations_payload()),
        ]
    )
    events: list[ProgressEvent] = []

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=events.append,
    )

    assert isinstance(result, RecommendationList)
    assert _pairs(events) == [
        (ProgressPhase.understanding, 0),
        (ProgressPhase.searching, 1),
        (ProgressPhase.composing, 2),
    ]


# ------------------------------------------- 2. comparison (search then compare)


def test_comparison_emits_understanding_searching_comparing_composing(
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
    events: list[ProgressEvent] = []

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=events.append,
    )

    assert isinstance(result, ComparativeAnalysis)
    assert _pairs(events) == [
        (ProgressPhase.understanding, 0),
        (ProgressPhase.searching, 1),
        (ProgressPhase.comparing, 2),
        (ProgressPhase.composing, 3),
    ]


# ------------------------------------------------- 3. guardrail refusal


def test_guardrail_emits_understanding_refused(config: BedrockConfig, kb_config: KBConfig) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
    client = CapturingConverseClient([converse_guardrail()])
    events: list[ProgressEvent] = []

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=events.append,
    )

    assert isinstance(result, RefusalResponse)
    assert _pairs(events) == [
        (ProgressPhase.understanding, 0),
        (ProgressPhase.refused, 1),
    ]


# ------------------------------------------- 4. non-tool_use stop mid-loop (bounded)


def test_non_tool_use_stop_emits_understanding_bounded_mid_loop(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository()
    kb = FakeAgentRuntime()
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
    events: list[ProgressEvent] = []

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=events.append,
    )

    assert isinstance(result, BoundedResponse)
    assert result.turns_used == 1
    assert _pairs(events) == [
        (ProgressPhase.understanding, 0),
        (ProgressPhase.bounded, 1),
    ]


# ------------------------------------------- 5. never-final tool loop, post-loop bounded


def test_never_final_tool_loop_emits_searching_each_turn_then_post_loop_bounded(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id=f"t{i}")
            )
            for i in range(5)
        ]
    )
    events: list[ProgressEvent] = []

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=events.append,
        max_turns=3,
    )

    assert isinstance(result, BoundedResponse)
    assert result.turns_used == 3
    assert _pairs(events) == [
        (ProgressPhase.understanding, 0),
        (ProgressPhase.searching, 1),
        (ProgressPhase.searching, 2),
        (ProgressPhase.searching, 3),
        (ProgressPhase.bounded, 3),
    ]


# ------------------------------------------- 6. a raising callback never breaks the run


def test_raising_callback_does_not_break_the_run(
    config: BedrockConfig, kb_config: KBConfig
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    kb = FakeAgentRuntime()
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(_recommendations_payload()),
        ]
    )

    def _boom(_event: ProgressEvent) -> None:
        raise RuntimeError("callback blew up")

    result = _run(
        client,
        repository=repo,
        kb_client=kb,
        config=config,
        kb_config=kb_config,
        on_progress=_boom,
    )

    # The callback raised on every emit, yet the agent run completed with the real result.
    assert isinstance(result, RecommendationList)
    assert result.kind == "recommendations"
