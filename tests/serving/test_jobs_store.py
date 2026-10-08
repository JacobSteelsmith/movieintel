"""JobStore round-trip tests (async 202 + poll path, design Decision 2).

Drives the real :class:`JobStore` against a moto ``MovieIntel`` table (the ``moto_job_store``
fixture) to lock: the create -> get round trip; the ``mark_running`` transition; the read-path
projection + ``Decimal`` normalization (``progress.turn`` reads back as an ``int``); the
``AgentResult`` alias-as-field lossless round trip (a ``RecommendationList`` with a ``float``
``pes`` reads back equal, AC-4); the stored-extras projection; the ``ConditionExpression``
guard (a late progress write after a terminal mark is swallowed, status unchanged); the
``expires_at`` TTL attribute; and ``get`` of an unknown id -> ``None``.
"""

from __future__ import annotations

from movieintel.agent.progress import PHASE_LABELS, ProgressPhase
from movieintel.agent.response import RecommendationList, RecommendedMovie
from movieintel.domain.schemas import Mood, Sentiment
from movieintel.persistence.keys import job_pk, job_sk
from movieintel.serving.jobs import JobProgress, JobStatus, WorkerError
from movieintel.serving.jobs_store import JobStore


def _progress(phase: ProgressPhase, turn: int) -> JobProgress:
    return JobProgress(phase=phase, turn=turn, label=PHASE_LABELS[phase])


def test_create_get_round_trip(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="j1", query="recommend action", max_turns=None)
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.job_id == "j1"
    assert job.status == JobStatus.queued
    assert job.progress.phase == ProgressPhase.understanding
    assert job.progress.turn == 0
    assert job.result is None
    assert job.error is None


def test_mark_running_flips_status(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="j1", query="q", max_turns=None)
    moto_job_store.mark_running("j1")
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.status == JobStatus.running


def test_update_progress_turn_is_int(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="j1", query="q", max_turns=None)
    moto_job_store.update_progress("j1", _progress(ProgressPhase.searching, 2))
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.progress.phase == ProgressPhase.searching
    assert job.progress.turn == 2
    # Decimal -> int normalization through _from_dynamo (NOT an implicit pydantic coercion).
    assert isinstance(job.progress.turn, int)


def test_get_succeeds_despite_stored_extras(moto_job_store: JobStore) -> None:
    # The stored item carries PK/SK/query/max_turns/expires_at and NO job_id; the explicit
    # projection + job_id reconstruction keeps extra='forbid' validation safe.
    moto_job_store.create(job_id="j1", query="q", max_turns=8)
    raw = moto_job_store._table.get_item(Key={"PK": job_pk("j1"), "SK": job_sk()})["Item"]
    assert {"PK", "SK", "query", "max_turns", "expires_at"} <= set(raw)
    assert "job_id" not in raw
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.job_id == "j1"


def test_mark_succeeded_result_round_trip(moto_job_store: JobStore) -> None:
    original = RecommendationList(
        query_summary="action movies",
        movies=[
            RecommendedMovie(
                movie_id="1",
                title="A",
                sentiment=Sentiment.POSITIVE,
                pes=90.0,
                mood=Mood.UPLIFTING,
                rationale="r",
            )
        ],
    )
    moto_job_store.create(job_id="j1", query="q", max_turns=None)
    moto_job_store.mark_running("j1")
    moto_job_store.mark_succeeded("j1", original)
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.status == JobStatus.succeeded
    # Alias-as-field smart-union + Decimal/float lossless round trip (AC-4).
    assert job.result == original


def test_expires_at_present_on_stored_item(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="j1", query="q", max_turns=None)
    raw = moto_job_store._table.get_item(Key={"PK": job_pk("j1"), "SK": job_sk()})["Item"]
    assert "expires_at" in raw
    assert int(raw["expires_at"]) > 0


def test_progress_after_terminal_is_swallowed(moto_job_store: JobStore) -> None:
    moto_job_store.create(job_id="j1", query="q", max_turns=None)
    moto_job_store.mark_running("j1")
    moto_job_store.mark_failed("j1", WorkerError(message="boom"))
    # A stray late progress write after a terminal mark is rejected by the guard and
    # swallowed at warning; status is unchanged.
    moto_job_store.update_progress("j1", _progress(ProgressPhase.searching, 5))
    job = moto_job_store.get("j1")
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.error is not None
    assert job.error.message == "boom"
    assert job.progress.turn != 5


def test_get_unknown_id_is_none(moto_job_store: JobStore) -> None:
    assert moto_job_store.get("does-not-exist") is None
