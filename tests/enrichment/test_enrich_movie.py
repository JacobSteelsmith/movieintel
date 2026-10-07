"""Enrichment client tests with mocked Bedrock (REQ-A-4, REQ-X-3.4, REQ-X-4.1/.3).

All Bedrock calls are mocked — either a botocore ``Stubber`` wrapping a real
``bedrock-runtime`` client (so the request shape, incl. ``guardrailConfig`` and
``modelId``, is asserted against the actual API contract) or a request-capturing
fake client that scripts multi-turn Converse responses for the repair/guardrail paths.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from movieintel.config import BedrockConfig
from movieintel.data_access.models import MovieRow, SampledMovie
from movieintel.domain.schemas import EffectivenessScore, EnrichmentAttributes, Mood
from movieintel.enrichment import enrich_movie
from movieintel.enrichment.results import Blocked, Failed, Ok, Repaired

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def config() -> BedrockConfig:
    """A fully configured BedrockConfig (model id + Guardrail) for the client."""
    return BedrockConfig.from_env(
        env={
            "BEDROCK_MODEL_ID": "anthropic.claude-test-v1:0",
            "BEDROCK_GUARDRAIL_ID": "gr-test",
            "BEDROCK_GUARDRAIL_VERSION": "DRAFT",
        }
    )


@pytest.fixture
def movie() -> SampledMovie:
    return SampledMovie(
        movie=MovieRow(
            movie_id=42,
            title="The Example",
            overview="A test film about testing.",
            genres='["Drama"]',
            language="en",
            release_date="2020-01-01",
            budget=1_000_000,
            revenue=5_000_000,
            runtime=120.0,
        ),
        rating=4.0,
        rating_count=10,
    )


@pytest.fixture
def score() -> EffectivenessScore:
    """Deterministic PES — the value the LLM must not alter."""
    return EffectivenessScore(value=84.85, roi_defined=True, rating_defined=True)


def valid_payload() -> dict[str, Any]:
    """A schema-valid EnrichmentAttributes payload (the model echoes PES fields)."""
    return {
        "overview_sentiment": "positive",
        "budget_tier": "medium",
        "revenue_tier": "high",
        "effectiveness": {
            "value": 84.85,
            "tier": "standout",
            "explanation": "Strong ROI and solid audience rating.",
            "roi_defined": True,
            "rating_defined": True,
        },
        "mood": "uplifting",
        "reasoning_basis": "Revenue well above budget; positive overview.",
    }


def converse_response(
    text: str,
    *,
    stop_reason: str = "end_turn",
    guardrail: bool = False,
) -> dict[str, Any]:
    """Build a realistic Converse response envelope."""
    usage = {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30}
    metrics = {"latencyMs": 100}
    if guardrail:
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": ""}]}},
            "stopReason": "guardrail_intervened",
            "usage": usage,
            "metrics": metrics,
            "trace": {"guardrail": {"outputAssessments": {}}},
        }
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": usage,
        "metrics": metrics,
    }


class _CapturingClient:
    """A scripted Converse client that records every request it receives."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("converse called more times than scripted")
        return self._responses.pop(0)


# ------------------------------------------------------------------- valid path (Ok)


def test_valid_path_returns_ok_with_stubbed_bedrock(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """Valid JSON -> Ok; stubbed real client asserts request shape via expected_params."""
    client = boto3.client(
        "bedrock-runtime",
        region_name=config.region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "converse",
            converse_response(json.dumps(valid_payload())),
            expected_params={
                "modelId": config.model_id,
                "guardrailConfig": {"guardrailIdentifier": "gr-test", "guardrailVersion": "DRAFT"},
                "system": ANY,
                "messages": ANY,
            },
        )
        result = enrich_movie(movie, score, client=client, config=config)
        stubber.assert_no_pending_responses()

    assert isinstance(result, Ok)
    assert isinstance(result.attributes, EnrichmentAttributes)
    assert result.attributes.mood in Mood
    # Deterministic PES preserved exactly.
    assert result.attributes.effectiveness.value == score.value
    assert result.attributes.effectiveness.roi_defined == score.roi_defined
    assert result.attributes.effectiveness.rating_defined == score.rating_defined


def test_valid_path_preserves_pes_even_when_model_lies(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """The model echoing a wrong PES value is overwritten by the deterministic score."""
    payload = valid_payload()
    payload["effectiveness"]["value"] = 1.23  # model attempts to alter the value
    payload["effectiveness"]["roi_defined"] = False
    payload["effectiveness"]["rating_defined"] = False
    client = _CapturingClient([converse_response(json.dumps(payload))])

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Ok)
    assert result.attributes.effectiveness.value == 84.85
    assert result.attributes.effectiveness.roi_defined is True
    assert result.attributes.effectiveness.rating_defined is True


# ----------------------------------------------------- malformed-then-repaired path


def test_malformed_then_repaired_returns_repaired_attempts_one(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """First response invalid, second valid -> Repaired(attempts=1), PES preserved."""
    bad = valid_payload()
    bad["overview_sentiment"] = "ecstatic"  # not in the Sentiment enum
    client = _CapturingClient(
        [
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(valid_payload())),
        ]
    )

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Repaired)
    assert result.attempts == 1
    assert result.attributes.effectiveness.value == score.value
    # The repair turn echoed the validation error back to the model.
    assert len(client.requests) == 2
    repair_msg = client.requests[1]["messages"][-1]["content"][0]["text"]
    assert "Validation error" in repair_msg


def test_malformed_json_is_repaired(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """Non-JSON output triggers the repair path too."""
    client = _CapturingClient(
        [
            converse_response("not json at all"),
            converse_response(json.dumps(valid_payload())),
        ]
    )
    result = enrich_movie(movie, score, client=client, config=config)
    assert isinstance(result, Repaired)
    assert result.attempts == 1


# ---------------------------------------------------------- repair-exhausted path


def test_repair_exhausted_returns_failed_without_raising(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """All responses invalid past MAX_REPAIR_ATTEMPTS -> Failed, no exception."""
    bad = valid_payload()
    bad["mood"] = "scary"  # not in the Mood enum
    # The default bound is now MAX_REPAIR_ATTEMPTS=4 -> 1 initial + 4 repairs = 5 calls.
    client = _CapturingClient([converse_response(json.dumps(bad)) for _ in range(5)])

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Failed)
    assert result.reason
    assert len(client.requests) == 5


def test_succeeds_on_fifth_call_only_under_new_bound(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """Four bad turns then a valid one -> Repaired(attempts=4) under the new bound of 4.

    Under the old bound of 2 this would have returned Failed; it exercises that the
    raised MAX_REPAIR_ATTEMPTS=4 recovers the dense-overview failures.
    """
    bad = valid_payload()
    bad["mood"] = "scary"  # not in the Mood enum
    client = _CapturingClient(
        [
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(valid_payload())),
        ]
    )

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Repaired)
    assert result.attempts == 4
    assert len(client.requests) == 5


def test_repair_attempts_override_is_honored(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """max_repair_attempts override bounds the number of Converse calls.

    The explicit override is intentionally independent of the MAX_REPAIR_ATTEMPTS module
    constant, so this test is unaffected by raising the default bound.
    """
    bad = valid_payload()
    bad["mood"] = "scary"
    client = _CapturingClient([converse_response(json.dumps(bad)) for _ in range(5)])

    result = enrich_movie(movie, score, client=client, config=config, max_repair_attempts=1)

    assert isinstance(result, Failed)
    assert len(client.requests) == 2  # 1 initial + 1 repair


# ---------------------------------------------------------------- guardrail path


def test_guardrail_blocked_returns_blocked_no_content(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """A guardrail intervention -> Blocked, no attributes, no unsafe content."""
    client = _CapturingClient([converse_response("", guardrail=True)])

    result = enrich_movie(movie, score, client=client, config=config)

    assert isinstance(result, Blocked)
    assert result.reason
    assert not hasattr(result, "attributes")
    assert len(client.requests) == 1  # blocked immediately, no repair turns


# -------------------------------------------------- guardrailConfig on every call


def test_guardrail_config_attached_on_every_request(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """Every Converse request — including each repair turn — carries guardrailConfig."""
    bad = valid_payload()
    bad["mood"] = "scary"
    client = _CapturingClient(
        [
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(valid_payload())),
        ]
    )

    enrich_movie(movie, score, client=client, config=config)

    assert len(client.requests) == 3
    for request in client.requests:
        assert request["guardrailConfig"] == {
            "guardrailIdentifier": "gr-test",
            "guardrailVersion": "DRAFT",
        }


# ------------------------------------------------------------- model id from config


def test_model_id_comes_from_config(
    movie: SampledMovie, score: EffectivenessScore, config: BedrockConfig
) -> None:
    """The modelId sent equals config.model_id (no hard-coded id in the client)."""
    client = _CapturingClient([converse_response(json.dumps(valid_payload()))])
    enrich_movie(movie, score, client=client, config=config)
    assert client.requests[0]["modelId"] == config.model_id == "anthropic.claude-test-v1:0"


def test_model_id_changes_with_env_override(movie: SampledMovie, score: EffectivenessScore) -> None:
    """Overriding the env var changes the modelId the client sends."""
    config = BedrockConfig.from_env(
        env={"BEDROCK_MODEL_ID": "anthropic.other-model:0", "BEDROCK_GUARDRAIL_ID": "gr-x"}
    )
    client = _CapturingClient([converse_response(json.dumps(valid_payload()))])
    enrich_movie(movie, score, client=client, config=config)
    assert client.requests[0]["modelId"] == "anthropic.other-model:0"
