"""Worker handler tests (async 202 + poll path, design Decision 3, FR-4/AC-9).

Drives the worker handler with an injected :class:`JobStore` (over a moto table), the
scripted :class:`CapturingConverseClient` + ``StubRepository``/``FakeAgentRuntime`` doubles,
and configured :class:`BedrockConfig`/:class:`KBConfig`. Asserts a successful run writes
``succeeded`` with the right result, that progress is written on a ``(phase, turn)`` change,
and that a deliberately-raising Converse client yields a ``failed`` record and NEVER an
escaped exception.
"""

from __future__ import annotations

from typing import Any

from handlers.worker.handler import handler

from movieintel.config import BedrockConfig
from movieintel.kb.config import KBConfig
from movieintel.serving.jobs import JobStatus
from movieintel.serving.jobs_store import JobStore
from tests.serving.conftest import (
    CapturingConverseClient,
    FakeAgentRuntime,
    StubRepository,
    converse_final,
    converse_tool_use,
    make_enriched,
    tool_use_block,
)


class _RaisingConverseClient:
    """A Converse client that raises on every call (simulates a Bedrock infra failure)."""

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("bedrock exploded")


def _run(
    *,
    job_store: JobStore,
    client: Any,
    repository: Any,
    kb: Any,
    bedrock_config: BedrockConfig,
    kb_config: KBConfig,
    job_id: str = "j1",
    query: str = "recommend action",
) -> dict[str, Any]:
    job_store.create(job_id=job_id, query=query, max_turns=None)
    return handler(
        {"job_id": job_id, "query": query},
        None,
        client=client,
        repository=repository,
        kb_client=kb,
        config=bedrock_config,
        kb_config=kb_config,
        job_store=job_store,
    )


def test_successful_run_writes_succeeded_with_result(
    moto_job_store: JobStore,
    bedrock_config: BedrockConfig,
    kb_config: KBConfig,
    recommendations_payload: dict[str, Any],
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(recommendations_payload),
        ]
    )

    result = _run(
        job_store=moto_job_store,
        client=client,
        repository=repo,
        kb=FakeAgentRuntime(),
        bedrock_config=bedrock_config,
        kb_config=kb_config,
    )

    assert result == {"job_id": "j1"}
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.status == JobStatus.succeeded
    assert job.result is not None
    assert job.result.kind == "recommendations"
    assert job.result.movies[0].title == "A Film"


def test_progress_written_on_phase_turn_change(
    moto_job_store: JobStore,
    bedrock_config: BedrockConfig,
    kb_config: KBConfig,
    recommendations_payload: dict[str, Any],
) -> None:
    repo = StubRepository(by_sentiment=[make_enriched(movie_id=1, pes_value=90.0)])
    client = CapturingConverseClient(
        [
            converse_tool_use(
                tool_use_block("query_movies", {"sentiment": "positive"}, tool_use_id="t1")
            ),
            converse_final(recommendations_payload),
        ]
    )

    _run(
        job_store=moto_job_store,
        client=client,
        repository=repo,
        kb=FakeAgentRuntime(),
        bedrock_config=bedrock_config,
        kb_config=kb_config,
    )

    # The terminal record is composing/turn 2 — proving progress advanced past the
    # create-time understanding/0 via the on_progress writer before the terminal write.
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.progress.phase.value == "composing"
    assert job.progress.turn == 2


def test_crashing_run_writes_failed_without_escaping(
    moto_job_store: JobStore,
    bedrock_config: BedrockConfig,
    kb_config: KBConfig,
) -> None:
    result = _run(
        job_store=moto_job_store,
        client=_RaisingConverseClient(),
        repository=StubRepository(),
        kb=FakeAgentRuntime(),
        bedrock_config=bedrock_config,
        kb_config=kb_config,
    )

    # No exception escaped; a terminal failed record exists (FR-4/AC-9).
    assert result == {"job_id": "j1"}
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.error is not None
    assert job.error.message == "unexpected error running the agent"
