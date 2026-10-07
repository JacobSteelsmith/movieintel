"""Contract tests: each toolConfig inputSchema matches its args model (REQ-B-2.4, X-3.2).

The regression guard against spec/handler drift. For every tool the hand-written Converse
``toolSpec.inputSchema.json`` (design §3.6, in :mod:`specs`) is checked field-by-field
against the Pydantic args model that actually validates the arguments:

- property-name set matches the model's field set;
- ``required`` matches the model's required (no-default) fields;
- each schema ``enum`` matches the model field's allowed values;
- numeric bounds (``minimum``/``maximum``, ``minItems``) match the model constraints;
- ``additionalProperties: false`` is present ↔ the model forbids extras.

If a future change edits the schema or the model but not both, one of these fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from movieintel.agent.tools.args import (
    CompareMoviesArgs,
    QueryMoviesArgs,
    SemanticSearchArgs,
)
from movieintel.agent.tools.specs import ALL_TOOL_SPECS, by_name

CASES = [
    ("query_movies", QueryMoviesArgs),
    ("semantic_search", SemanticSearchArgs),
    ("compare_movies", CompareMoviesArgs),
]


def _schema(name: str) -> dict[str, Any]:
    """The ``inputSchema.json`` block of the named toolSpec."""
    schema: dict[str, Any] = by_name(name)["toolSpec"]["inputSchema"]["json"]
    return schema


def _model_fields(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def _model_required(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.is_required()}


def test_all_tool_specs_are_exactly_the_three_tools() -> None:
    names = {spec["toolSpec"]["name"] for spec in ALL_TOOL_SPECS}
    assert names == {"query_movies", "semantic_search", "compare_movies"}
    assert len(ALL_TOOL_SPECS) == 3


@pytest.mark.parametrize(("name", "model"), CASES)
def test_property_names_match_model_fields(name: str, model: type[BaseModel]) -> None:
    schema = _schema(name)
    assert set(schema["properties"]) == _model_fields(model)


@pytest.mark.parametrize(("name", "model"), CASES)
def test_additional_properties_false_matches_extra_forbid(
    name: str, model: type[BaseModel]
) -> None:
    schema = _schema(name)
    assert schema.get("additionalProperties") is False
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(("name", "model"), CASES)
def test_required_matches_model_required(name: str, model: type[BaseModel]) -> None:
    schema = _schema(name)
    assert set(schema.get("required", [])) == _model_required(model)


def test_query_movies_enums_and_bounds() -> None:
    schema = _schema("query_movies")
    assert set(schema["properties"]["sentiment"]["enum"]) == {"positive", "negative", "neutral"}
    assert set(schema["properties"]["sort_by"]["enum"]) == {"pes", "revenue", "budget", "runtime"}
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 50
    # No field is required (every arg is optional on query_movies).
    assert "required" not in schema or schema["required"] == []


def test_semantic_search_bounds_and_required() -> None:
    schema = _schema("semantic_search")
    assert schema["required"] == ["query"]
    assert schema["properties"]["top_k"]["minimum"] == 1
    assert schema["properties"]["top_k"]["maximum"] == 20


def test_compare_movies_enums_and_bounds() -> None:
    schema = _schema("compare_movies")
    assert schema["required"] == ["movie_ids"]
    assert schema["properties"]["movie_ids"]["minItems"] == 2
    assert set(schema["properties"]["dimensions"]["items"]["enum"]) == {
        "budget",
        "revenue",
        "runtime",
        "pes",
    }
