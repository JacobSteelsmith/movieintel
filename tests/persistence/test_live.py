"""Live-AWS smoke tests (``@pytest.mark.live``, skipped by default — REQ-X-3.5).

These hit real AWS and are collected-but-skipped in CI. Run with ``-m live`` and real
credentials/resources to exercise the real-boto3 paths.
"""

from __future__ import annotations

import boto3
import pytest

from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.vectors import TitanEmbedder


@pytest.mark.live
def test_titan_embedder_returns_vector_against_real_bedrock() -> None:
    """Real Titan Text Embeddings V2 returns a non-empty float vector."""
    session = boto3.Session()
    try:
        credentials = session.get_credentials()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"AWS credentials unavailable; skipping live Titan embedding call: {exc}")
    if credentials is None:
        pytest.skip("No AWS credentials configured; skipping live Titan embedding call.")

    config = PersistenceConfig.from_env()
    client = session.client("bedrock-runtime", region_name=config.region)
    embedder = TitanEmbedder(client=client, config=config)
    vector = embedder.embed_overview("A tense heist thriller set in Tokyo.")
    assert len(vector) > 0
    assert all(isinstance(x, float) for x in vector)
