"""Converse ``toolConfig`` ``toolSpec`` dicts for the three tools (design §3.6).

These JSON schemas are copied verbatim from design §3.6 and are the contract the Bedrock
Converse API sees. They are kept in exact sync with the Pydantic args models in
:mod:`args` (property names, enums, required fields, numeric bounds,
``additionalProperties: false`` ↔ ``extra="forbid"``); the contract tests in
``tests/contract/test_tool_specs.py`` enforce that sync so a spec/handler drift fails CI.

``ALL_TOOL_SPECS`` is the list a Converse ``toolConfig`` embeds; ``by_name`` looks a spec
up by its ``toolSpec.name``.
"""

from __future__ import annotations

from typing import Any

QUERY_MOVIES_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "query_movies",
        "description": (
            "Filter enriched movies by sentiment and numeric ranges (budget, revenue, "
            "runtime), optionally ranked by Production Effectiveness Score."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                    },
                    "min_budget": {"type": "number"},
                    "max_budget": {"type": "number"},
                    "min_revenue": {"type": "number"},
                    "max_revenue": {"type": "number"},
                    "min_runtime": {"type": "number"},
                    "max_runtime": {"type": "number"},
                    "genres": {"type": "array", "items": {"type": "string"}},
                    "sort_by": {
                        "type": "string",
                        "enum": ["pes", "revenue", "budget", "runtime"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            }
        },
    }
}

SEMANTIC_SEARCH_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "semantic_search",
        "description": (
            "Semantic retrieval over enriched movie overviews via the Bedrock Knowledge Base."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            }
        },
    }
}

COMPARE_MOVIES_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "compare_movies",
        "description": (
            "Deterministic side-by-side comparison of movies across budget, revenue, "
            "runtime, and PES."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "movie_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["budget", "revenue", "runtime", "pes"],
                        },
                    },
                },
                "required": ["movie_ids"],
                "additionalProperties": False,
            }
        },
    }
}

ALL_TOOL_SPECS: list[dict[str, Any]] = [
    QUERY_MOVIES_SPEC,
    SEMANTIC_SEARCH_SPEC,
    COMPARE_MOVIES_SPEC,
]


def by_name(name: str) -> dict[str, Any]:
    """Return the ``toolSpec`` dict whose ``toolSpec.name`` equals ``name``.

    :raises KeyError: when no spec with that name exists.
    """
    for spec in ALL_TOOL_SPECS:
        if spec["toolSpec"]["name"] == name:
            return spec
    raise KeyError(name)
