"""Extract handler tests against the real read-only ``_sqlite/`` source DBs.

The Extract handler is the only handler that reads the source SQLite files; it points at
them via the ``SQLITE_DIR`` env var (the real dir in tests, the mounted layer in prod).
It wires the data-access functions (open + resolve schema + observe scale + sample) and
returns ``{"movies": [sampled_json...], "scale": scale_json}`` — the Map input downstream.
"""

from __future__ import annotations

import pytest
from handlers.extract import handler as extract_handler
from handlers.extract.handler import SqliteDirNotConfiguredError
from handlers.shared import events

from movieintel.data_access.models import RatingScale, SampledMovie

# The real source DBs live in _sqlite/ (see tests/conftest.py); point SQLITE_DIR there.
from tests.conftest import MOVIES_DB_PATH


@pytest.fixture
def sqlite_dir(monkeypatch: pytest.MonkeyPatch) -> str:
    directory = str(MOVIES_DB_PATH.parent)
    monkeypatch.setenv("SQLITE_DIR", directory)
    return directory


def test_extract_returns_sampled_movies_and_scale(sqlite_dir: str) -> None:
    """A valid request samples N movies and derives the rating scale."""
    result = extract_handler.handler({"n": 50, "seed": 7}, None)

    assert set(result) == {"movies", "scale"}
    assert len(result["movies"]) == 50

    # Each movie round-trips back to a SampledMovie, and the scale to a RatingScale.
    for movie_json in result["movies"]:
        assert isinstance(events.sampled_from_json(movie_json), SampledMovie)
    scale = events.scale_from_json(result["scale"])
    assert isinstance(scale, RatingScale)
    assert scale.effective_max > 0


def test_extract_is_reproducible_for_a_seed(sqlite_dir: str) -> None:
    """Same seed -> identical sample (deterministic sampling, REQ-A-1.3)."""
    first = extract_handler.handler({"n": 50, "seed": 7}, None)
    second = extract_handler.handler({"n": 50, "seed": 7}, None)
    assert first == second


def test_extract_defaults_to_100_high_revenue_sample(sqlite_dir: str) -> None:
    """An input-less event defaults to the top-100 high-revenue eligible sample (D5)."""
    result = extract_handler.handler({}, None)
    assert len(result["movies"]) == 100


def test_extract_reads_sqlite_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/empty SQLITE_DIR is a configuration error surfaced loudly."""
    monkeypatch.delenv("SQLITE_DIR", raising=False)
    with pytest.raises(SqliteDirNotConfiguredError):
        extract_handler.handler({"n": 50, "seed": 7}, None)
