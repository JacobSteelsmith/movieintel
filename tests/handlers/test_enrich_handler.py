"""Enrich handler tests: wrap ``enrichment.enrich_movie`` with a scripted Converse client.

The handler builds a :class:`BedrockConfig` from the env (so ``guardrail_config()`` works
once ``BEDROCK_GUARDRAIL_ID`` is set), accepts an injected Converse client in tests, and
serializes the ``Ok|Repaired|Failed|Blocked`` outcome into the shared envelope. These
cover the four outcomes with the mocked Converse client and a configured Guardrail id.
"""

from __future__ import annotations

import json
import logging

import pytest
from handlers.enrich import handler as enrich_handler
from handlers.shared import events

from movieintel.domain.schemas import EnrichmentAttributes
from tests.handlers.conftest import (
    ScriptedConverseClient,
    converse_response,
    enrich_event,
    make_effectiveness,
    make_sampled,
    valid_attributes_payload,
)


@pytest.fixture(autouse=True)
def _guardrail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Guardrail id must be configured so every Converse call attaches a Guardrail."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-test-v1:0")


def test_enrich_ok_outcome() -> None:
    """Valid JSON on the first turn -> an 'ok' envelope carrying validated attributes."""
    client = ScriptedConverseClient([converse_response(json.dumps(valid_attributes_payload()))])
    event = enrich_event(sampled=make_sampled(), score=make_effectiveness())

    result = enrich_handler.handler(event, None, client=client)

    assert result["result"]["outcome"] == "ok"
    attributes = events.attributes_from_json(result["result"]["attributes"])
    assert isinstance(attributes, EnrichmentAttributes)
    # The movie and score pass through for the Validate state.
    assert result["movie"] == event["movie"]
    assert result["score"] == event["score"]


def test_enrich_repaired_outcome() -> None:
    """First response invalid, second valid -> a 'repaired' envelope with attempts >= 1."""
    bad = valid_attributes_payload()
    bad["overview_sentiment"] = "ecstatic"  # out of enum
    client = ScriptedConverseClient(
        [
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(valid_attributes_payload())),
        ]
    )

    result = enrich_handler.handler(enrich_event(), None, client=client)

    assert result["result"]["outcome"] == "repaired"
    assert result["result"]["attempts"] == 1
    assert result["result"]["attributes"] is not None


def test_enrich_failed_outcome() -> None:
    """Repair exhausted -> a 'failed' envelope with a reason and null attributes.

    The default repair bound is now ``MAX_REPAIR_ATTEMPTS=4`` (1 initial + 4 repairs = 5
    calls), so the client must script 5 all-bad responses for the handler path to exhaust.
    """
    bad = valid_attributes_payload()
    bad["mood"] = "scary"  # out of enum
    client = ScriptedConverseClient([converse_response(json.dumps(bad)) for _ in range(5)])

    result = enrich_handler.handler(enrich_event(), None, client=client)

    assert result["result"]["outcome"] == "failed"
    assert result["result"]["attributes"] is None
    assert result["result"]["reason"]


def test_enrich_blocked_outcome() -> None:
    """A Guardrail intervention -> a 'blocked' envelope with no attributes."""
    client = ScriptedConverseClient([converse_response("", guardrail=True)])

    result = enrich_handler.handler(enrich_event(), None, client=client)

    assert result["result"]["outcome"] == "blocked"
    assert result["result"]["attributes"] is None
    assert result["result"]["reason"]


def test_enrich_attaches_guardrail_on_every_call() -> None:
    """Every Converse request carries guardrailConfig (REQ-A-4.4)."""
    client = ScriptedConverseClient([converse_response(json.dumps(valid_attributes_payload()))])

    enrich_handler.handler(enrich_event(), None, client=client)

    assert client.requests
    for request in client.requests:
        assert request["guardrailConfig"] == {
            "guardrailIdentifier": "gr-test",
            "guardrailVersion": "DRAFT",
        }


# ------------------------------------------------------------- outcome logging (CHANGE 1)


def _outcome_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The INFO records the handler emitted on its own logger for the outcome line."""
    return [
        record
        for record in caplog.records
        if record.name == enrich_handler.logger.name
        and record.levelno == logging.INFO
        and "enrichment outcome" in record.getMessage()
    ]


def test_logs_ok_outcome_without_leaking_facts(caplog: pytest.LogCaptureFixture) -> None:
    """An 'ok' outcome emits exactly one INFO line with id + outcome, no movie facts."""
    client = ScriptedConverseClient([converse_response(json.dumps(valid_attributes_payload()))])
    event = enrich_event(sampled=make_sampled(), score=make_effectiveness())

    with caplog.at_level(logging.INFO, logger=enrich_handler.logger.name):
        result = enrich_handler.handler(event, None, client=client)

    records = _outcome_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "movie_id=42" in message
    assert "outcome=ok" in message
    assert "reason=-" in message
    # No movie content leaks into the log line.
    assert "A test film about testing." not in message
    assert "The Example" not in message
    # The returned payload is unchanged.
    assert result["result"]["outcome"] == "ok"
    assert result["movie"] == event["movie"]
    assert result["score"] == event["score"]


def test_logs_repaired_outcome_with_attempts(caplog: pytest.LogCaptureFixture) -> None:
    """A 'repaired' outcome logs attempts=1 and no movie facts."""
    bad = valid_attributes_payload()
    bad["overview_sentiment"] = "ecstatic"  # out of enum
    client = ScriptedConverseClient(
        [
            converse_response(json.dumps(bad)),
            converse_response(json.dumps(valid_attributes_payload())),
        ]
    )

    with caplog.at_level(logging.INFO, logger=enrich_handler.logger.name):
        enrich_handler.handler(enrich_event(), None, client=client)

    records = _outcome_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "movie_id=42" in message
    assert "outcome=repaired" in message
    assert "attempts=1" in message
    assert "A test film about testing." not in message


def test_logs_failed_outcome_with_short_reason(caplog: pytest.LogCaptureFixture) -> None:
    """A 'failed' outcome logs a non-empty, bounded reason (<= 200 chars + ellipsis)."""
    bad = valid_attributes_payload()
    bad["mood"] = "scary"  # out of enum
    client = ScriptedConverseClient([converse_response(json.dumps(bad)) for _ in range(5)])

    with caplog.at_level(logging.INFO, logger=enrich_handler.logger.name):
        enrich_handler.handler(enrich_event(), None, client=client)

    records = _outcome_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "movie_id=42" in message
    assert "outcome=failed" in message
    reason = message.split("reason=", 1)[1]
    assert reason and reason != "-"
    assert len(reason.removesuffix("…")) <= enrich_handler._MAX_REASON_LEN


def test_logs_blocked_outcome_with_reason(caplog: pytest.LogCaptureFixture) -> None:
    """A 'blocked' outcome logs a non-empty reason."""
    client = ScriptedConverseClient([converse_response("", guardrail=True)])

    with caplog.at_level(logging.INFO, logger=enrich_handler.logger.name):
        enrich_handler.handler(enrich_event(), None, client=client)

    records = _outcome_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "movie_id=42" in message
    assert "outcome=blocked" in message
    reason = message.split("reason=", 1)[1]
    assert reason and reason != "-"
