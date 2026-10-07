"""Config tests: defaults, env overrides, Guardrail config (design §1.3, §11)."""

from __future__ import annotations

import pytest

from movieintel.config import (
    DEFAULT_MODEL_ID,
    DEFAULT_REGION,
    BedrockConfig,
    GuardrailNotConfiguredError,
)


def test_from_env_defaults_when_unset() -> None:
    config = BedrockConfig.from_env(env={})
    assert config.region == DEFAULT_REGION == "us-east-1"
    assert config.model_id == DEFAULT_MODEL_ID
    assert config.guardrail_id is None
    assert config.guardrail_version == "DRAFT"


def test_from_env_honors_overrides() -> None:
    config = BedrockConfig.from_env(
        env={
            "BEDROCK_REGION": "us-west-2",
            "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
            "BEDROCK_GUARDRAIL_ID": "gr-123",
            "BEDROCK_GUARDRAIL_VERSION": "7",
        }
    )
    assert config.region == "us-west-2"
    assert config.model_id == "anthropic.claude-3-haiku-20240307-v1:0"
    assert config.guardrail_id == "gr-123"
    assert config.guardrail_version == "7"


def test_from_env_region_falls_back_to_aws_region() -> None:
    config = BedrockConfig.from_env(env={"AWS_REGION": "eu-central-1"})
    assert config.region == "eu-central-1"


def test_from_env_prefers_bedrock_region_over_aws_region() -> None:
    config = BedrockConfig.from_env(
        env={"BEDROCK_REGION": "us-east-2", "AWS_REGION": "eu-central-1"}
    )
    assert config.region == "us-east-2"


def test_from_env_reads_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model-from-process-env")
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    config = BedrockConfig.from_env()
    assert config.model_id == "model-from-process-env"


def test_guardrail_config_returns_dict_when_id_set() -> None:
    config = BedrockConfig.from_env(
        env={"BEDROCK_GUARDRAIL_ID": "gr-xyz", "BEDROCK_GUARDRAIL_VERSION": "3"}
    )
    assert config.guardrail_config() == {
        "guardrailIdentifier": "gr-xyz",
        "guardrailVersion": "3",
    }


def test_guardrail_config_raises_when_id_unset() -> None:
    config = BedrockConfig.from_env(env={})
    with pytest.raises(GuardrailNotConfiguredError):
        config.guardrail_config()
