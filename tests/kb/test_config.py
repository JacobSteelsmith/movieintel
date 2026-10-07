"""KBConfig tests: documented defaults + env overrides, KB id enforcement (REQ-B-1)."""

from __future__ import annotations

import pytest

from movieintel.kb.config import (
    DEFAULT_NUMBER_OF_RESULTS,
    DEFAULT_REGION,
    KBConfig,
    KnowledgeBaseNotConfiguredError,
)


def test_defaults_when_unset() -> None:
    config = KBConfig.from_env(env={})
    assert config.region == DEFAULT_REGION == "us-east-1"
    assert config.knowledge_base_id is None
    assert config.number_of_results == DEFAULT_NUMBER_OF_RESULTS == 5


def test_env_overrides() -> None:
    config = KBConfig.from_env(
        env={
            "MOVIEINTEL_KB_ID": "kb-abc123",
            "MOVIEINTEL_KB_NUM_RESULTS": "12",
        }
    )
    assert config.knowledge_base_id == "kb-abc123"
    assert config.number_of_results == 12


def test_region_prefers_bedrock_region_over_aws_region() -> None:
    config = KBConfig.from_env(env={"BEDROCK_REGION": "us-east-2", "AWS_REGION": "eu-central-1"})
    assert config.region == "us-east-2"


def test_region_falls_back_to_aws_region() -> None:
    config = KBConfig.from_env(env={"AWS_REGION": "us-west-2"})
    assert config.region == "us-west-2"


def test_reads_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVIEINTEL_KB_ID", "kb-from-process-env")
    config = KBConfig.from_env()
    assert config.knowledge_base_id == "kb-from-process-env"


def test_require_knowledge_base_id_returns_id_when_set() -> None:
    config = KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "kb-xyz"})
    assert config.require_knowledge_base_id() == "kb-xyz"


def test_require_knowledge_base_id_raises_when_unset() -> None:
    config = KBConfig.from_env(env={})
    with pytest.raises(KnowledgeBaseNotConfiguredError):
        config.require_knowledge_base_id()
