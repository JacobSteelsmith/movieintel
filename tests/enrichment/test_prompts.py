"""Prompt-builder tests pinning the strengthened structured-output instructions.

These assert on intent/substrings (case-insensitively where sensible) rather than exact
wording so they stay robust to small edits: the system/user prompts must demand a single
JSON object with no prose/markdown fences, every required field, and concise strings; the
repair message must echo the specific validation error and re-state the JSON-only contract.
The deterministic PES value and the embedded schema must still appear in the user message.
"""

from __future__ import annotations

from movieintel.data_access.models import MovieRow, SampledMovie
from movieintel.enrichment.prompts import (
    build_repair_message,
    build_system_prompt,
    build_user_message,
)


def _movie() -> SampledMovie:
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


def test_system_prompt_demands_json_only_no_fences() -> None:
    """The system prompt forbids prose/markdown fences and asks for one JSON object."""
    prompt = build_system_prompt().lower()
    assert "json" in prompt
    assert "only" in prompt
    assert "fence" in prompt or "markdown" in prompt
    assert "prose" in prompt


def test_system_prompt_requires_every_field_and_concise_strings() -> None:
    """The system prompt instructs including every required field and concise strings."""
    prompt = build_system_prompt().lower()
    assert "every" in prompt and "field" in prompt
    assert "concise" in prompt or "short" in prompt


def test_user_message_embeds_pes_value_and_schema() -> None:
    """The user message still states the deterministic PES value and embeds the schema."""
    message = build_user_message(_movie(), pes_value=84.85)
    assert "84.85" in message
    # The embedded JSON Schema is present (its "$defs"/"properties" keys appear).
    assert "properties" in message


def test_user_message_demands_json_only_for_long_overviews() -> None:
    """The user message re-states JSON-only output and concise fields for long overviews."""
    message = build_user_message(_movie(), pes_value=84.85).lower()
    assert "only" in message and "json" in message
    assert "fence" in message or "markdown" in message
    assert "concise" in message or "short" in message


def test_repair_message_echoes_error_and_restates_json_contract() -> None:
    """The repair message echoes the specific error and re-states the JSON-only contract."""
    err = "effectiveness.tier: value is not a valid enumeration member"
    message = build_repair_message(err)
    assert err in message
    lowered = message.lower()
    assert "only" in lowered and "json" in lowered
    assert "fence" in lowered or "markdown" in lowered
    assert "every" in lowered and "field" in lowered
