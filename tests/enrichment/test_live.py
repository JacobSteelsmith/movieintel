"""Opt-in live single-movie enrichment check (REQ-X-3.5).

Gated behind the ``live`` marker so it never runs in CI (run with ``-m live``). Skips
cleanly when no Guardrail id is configured, since a Guardrail is required on every call.
"""

from __future__ import annotations

import boto3
import pytest

from movieintel.config import BedrockConfig
from movieintel.data_access.models import MovieRow, SampledMovie
from movieintel.domain.schemas import EffectivenessScore
from movieintel.enrichment import enrich_movie
from movieintel.enrichment.results import Ok, Repaired


@pytest.mark.live
def test_enrich_single_movie_live() -> None:
    config = BedrockConfig.from_env()
    if config.guardrail_id is None:
        pytest.skip("No BEDROCK_GUARDRAIL_ID configured; skipping live enrichment.")

    client = boto3.client("bedrock-runtime", region_name=config.region)
    movie = SampledMovie(
        movie=MovieRow(
            movie_id=1,
            title="Live Smoke Test",
            overview="A small, hopeful story about an engineer shipping a feature.",
            genres='["Drama"]',
            language="en",
            release_date="2021-06-01",
            budget=2_000_000,
            revenue=9_000_000,
            runtime=105.0,
        ),
        rating=4.2,
        rating_count=25,
    )
    score = EffectivenessScore(value=82.5, roi_defined=True, rating_defined=True)

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Ok | Repaired)
    assert result.attributes.effectiveness.value == score.value
