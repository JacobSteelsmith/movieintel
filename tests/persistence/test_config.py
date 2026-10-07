"""PersistenceConfig tests: documented defaults + env overrides (no inline literals)."""

from __future__ import annotations

import pytest

from movieintel.persistence.config import (
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_GSI1_NAME,
    DEFAULT_REGION,
    DEFAULT_TABLE_NAME,
    DEFAULT_VECTOR_BUCKET_NAME,
    DEFAULT_VECTOR_INDEX_NAME,
    PersistenceConfig,
)


def test_defaults_when_unset() -> None:
    config = PersistenceConfig.from_env(env={})
    assert config.region == DEFAULT_REGION == "us-east-1"
    assert config.table_name == DEFAULT_TABLE_NAME == "MovieIntel"
    assert config.gsi1_name == DEFAULT_GSI1_NAME == "GSI1"
    assert config.vector_bucket_name == DEFAULT_VECTOR_BUCKET_NAME == "movieintel-vectors"
    assert config.vector_index_name == DEFAULT_VECTOR_INDEX_NAME == "movieintel-overviews"
    assert config.embedding_model_id == DEFAULT_EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0"


def test_each_name_honors_its_env_override() -> None:
    config = PersistenceConfig.from_env(
        env={
            "MOVIEINTEL_TABLE_NAME": "OtherTable",
            "MOVIEINTEL_GSI1_NAME": "AltIndex",
            "MOVIEINTEL_VECTOR_BUCKET": "other-vectors",
            "MOVIEINTEL_VECTOR_INDEX": "other-overviews",
            "MOVIEINTEL_EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0-custom",
        }
    )
    assert config.table_name == "OtherTable"
    assert config.gsi1_name == "AltIndex"
    assert config.vector_bucket_name == "other-vectors"
    assert config.vector_index_name == "other-overviews"
    assert config.embedding_model_id == "amazon.titan-embed-text-v2:0-custom"


def test_region_prefers_bedrock_region_over_aws_region() -> None:
    config = PersistenceConfig.from_env(
        env={"BEDROCK_REGION": "us-east-2", "AWS_REGION": "eu-central-1"}
    )
    assert config.region == "us-east-2"


def test_region_falls_back_to_aws_region() -> None:
    config = PersistenceConfig.from_env(env={"AWS_REGION": "us-west-2"})
    assert config.region == "us-west-2"


def test_reads_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVIEINTEL_TABLE_NAME", "FromProcessEnv")
    config = PersistenceConfig.from_env()
    assert config.table_name == "FromProcessEnv"
