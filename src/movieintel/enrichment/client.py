"""Bedrock Converse enrichment client: structured output + repair + Guardrails (REQ-A-4).

``enrich_movie`` drives a single movie through the design §3.3 flow:

1. Build a Converse request (system prompt + user message) with ``modelId`` and
   ``guardrailConfig`` from :class:`BedrockConfig` — a Guardrail is attached on EVERY
   call, including each repair turn (REQ-A-4.4, REQ-X-4.1).
2. If the response signals a Guardrail intervention, return :class:`Blocked` without
   parsing or returning any content (REQ-A-4.5, REQ-X-4.3).
3. Otherwise extract the text block, parse JSON, overwrite the deterministic PES fields
   from the passed-in score (the LLM cannot alter them, REQ-A-3.4), and validate against
   :class:`EnrichmentAttributes` (REQ-A-4.2, REQ-X-1.2).
4. On JSON/validation failure, run a bounded repair loop up to ``MAX_REPAIR_ATTEMPTS``
   (REQ-A-4.3). On exhaustion, return :class:`Failed` — never raise/abort.

Bedrock is reached through the :class:`BedrockConverseClient` Protocol; boto3's
``bedrock-runtime`` client satisfies it structurally, and tests drive a stubbed client.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import ValidationError

from movieintel.config import BedrockConfig
from movieintel.data_access.models import SampledMovie
from movieintel.domain.schemas import EffectivenessScore, EnrichmentAttributes
from movieintel.enrichment.prompts import (
    build_repair_message,
    build_system_prompt,
    build_user_message,
)
from movieintel.enrichment.results import Blocked, EnrichmentResult, Failed, Ok, Repaired

# Configured maximum number of bounded repair turns (REQ-A-4.3). Overridable per call
# via ``enrich_movie(..., max_repair_attempts=...)`` and as a module constant. Raised from
# 2 to 4 to lift the structured-output pass rate on long/dense overviews.
MAX_REPAIR_ATTEMPTS = 4

# Converse ``stopReason`` emitted when a Guardrail intervenes.
_GUARDRAIL_STOP_REASON = "guardrail_intervened"


class BedrockConverseClient(Protocol):
    """Structural interface for the Bedrock ``Converse`` call (design §3.3, §8)."""

    def converse(self, **kwargs: Any) -> dict[str, Any]: ...


class _StructuredOutputError(Exception):
    """Internal: the model output could not be parsed/validated this turn."""


def _is_guardrail_intervention(response: dict[str, Any]) -> bool:
    """True when the Converse response indicates a Guardrail blocked the generation."""
    return response.get("stopReason") == _GUARDRAIL_STOP_REASON


def _extract_text(response: dict[str, Any]) -> str:
    """Extract the first text content block from a Converse response.

    :raises _StructuredOutputError: when no text content block is present.
    """
    content = response.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text
    raise _StructuredOutputError("Response contained no text content block.")


def _parse_and_validate(text: str, score: EffectivenessScore) -> EnrichmentAttributes:
    """Parse model JSON and validate it, preserving the deterministic PES fields.

    The model-supplied ``effectiveness.value``/``roi_defined``/``rating_defined`` are
    overwritten with the deterministic values from ``score`` before validation so the
    final attributes always carry the computed PES unchanged (REQ-A-3.4).

    :raises _StructuredOutputError: on malformed JSON or schema-validation failure.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _StructuredOutputError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise _StructuredOutputError("Top-level JSON value must be an object.")

    effectiveness = payload.get("effectiveness")
    if not isinstance(effectiveness, dict):
        effectiveness = {}
        payload["effectiveness"] = effectiveness
    # The LLM reasons about a value it cannot change: pin the deterministic fields.
    effectiveness["value"] = score.value
    effectiveness["roi_defined"] = score.roi_defined
    effectiveness["rating_defined"] = score.rating_defined

    try:
        return EnrichmentAttributes.model_validate(payload)
    except ValidationError as exc:
        raise _StructuredOutputError(str(exc)) from exc


def _converse_request(
    *,
    config: BedrockConfig,
    messages: list[dict[str, Any]],
    system_prompt: str,
) -> dict[str, Any]:
    """Assemble a Converse request with the Guardrail attached on every call."""
    return {
        "modelId": config.model_id,
        "system": [{"text": system_prompt}],
        "messages": messages,
        "guardrailConfig": config.guardrail_config(),
    }


def enrich_movie(
    movie: SampledMovie,
    score: EffectivenessScore,
    *,
    client: BedrockConverseClient,
    config: BedrockConfig,
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> EnrichmentResult:
    """Enrich a single movie via Bedrock Converse (design §3.3).

    ``score`` is the deterministic PES computed in ``domain``; its
    ``value``/``roi_defined``/``rating_defined`` are preserved byte-for-byte in the
    returned attributes. Returns :class:`Ok`, :class:`Repaired`, :class:`Failed`, or
    :class:`Blocked`; a per-movie problem is recorded as a result, never raised.
    """
    system_prompt = build_system_prompt()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": build_user_message(movie, score.value)}]}
    ]

    attempt = 0
    while True:
        response = client.converse(
            **_converse_request(config=config, messages=messages, system_prompt=system_prompt)
        )

        # Guardrail interventions are checked first — never parse blocked content.
        if _is_guardrail_intervention(response):
            return Blocked(reason="Bedrock Guardrail intervened on the enrichment request.")

        try:
            attributes = _parse_and_validate(_extract_text(response), score)
        except _StructuredOutputError as exc:
            if attempt >= max_repair_attempts:
                return Failed(
                    reason=(f"Structured output invalid after {attempt} repair attempt(s): {exc}")
                )
            # Record the model's turn, then ask for a bounded correction.
            messages.append(_assistant_turn(response))
            messages.append({"role": "user", "content": [{"text": build_repair_message(str(exc))}]})
            attempt += 1
            continue

        if attempt == 0:
            return Ok(attributes=attributes)
        return Repaired(attributes=attributes, attempts=attempt)


def _assistant_turn(response: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the assistant message to keep the repair conversation coherent."""
    message = response.get("output", {}).get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return {"role": "assistant", "content": message.get("content", [])}
    return {"role": "assistant", "content": [{"text": ""}]}
