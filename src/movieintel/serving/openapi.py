"""OpenAPI 3.1 generation from the serving Pydantic models (Task 10 FEAT-001, REQ-B-4.2).

``docs/openapi.yaml`` is a PUBLISHED artifact built from the single source of truth - the
Pydantic models - via ``model_json_schema()``. :func:`build_openapi` assembles an OpenAPI
3.1 document documenting:

- POST ``/query`` - the synchronous endpoint: a request body referencing
  :class:`~movieintel.serving.schemas.QueryRequest`, a 200 response that is the
  ``AgentResult`` union (``oneOf`` over the five result models, discriminated by ``kind``),
  and a 400 response referencing
  :class:`~movieintel.serving.schemas.ValidationErrorResponse`.
- POST ``/jobs`` - submit an async job: the same ``QueryRequest`` body, a 202 response
  referencing :class:`~movieintel.serving.jobs.SubmitResponse`, and a 400 response
  referencing ``ValidationErrorResponse``.
- GET ``/jobs/{id}`` - poll a job: a path parameter ``id``, a 200 response referencing
  :class:`~movieintel.serving.jobs.JobStatusResponse` (which embeds the ``AgentResult``
  ``oneOf`` via its ``result`` field), and a 404 response referencing
  :class:`~movieintel.serving.jobs.NotFoundResponse`.

``/health`` is intentionally omitted. All component schemas are harvested from the models'
JSON Schemas (including their nested ``$defs``) so the document is self-contained. The
document is rendered as JSON-compatible YAML (JSON is a strict subset of YAML 1.2), so it
parses with the stdlib ``json`` module and needs no new YAML dependency.
:func:`write_openapi_yaml` writes it to :data:`OPENAPI_PATH`, and a sync test asserts the
on-disk file matches the generator output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from movieintel.agent.response import (
    BoundedResponse,
    ComparativeAnalysis,
    PreferenceSummary,
    RecommendationList,
    RefusalResponse,
)
from movieintel.serving.jobs import (
    JobProgress,
    JobStatusResponse,
    NotFoundResponse,
    SubmitResponse,
    WorkerError,
)
from movieintel.serving.schemas import QueryRequest, ValidationErrorResponse

# Repo root is three parents up from this file (src/movieintel/serving/openapi.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: Published OpenAPI artifact location (regenerated when the models change).
OPENAPI_PATH = str(_REPO_ROOT / "docs" / "openapi.yaml")

# The five AgentResult member models, in a stable order (REQ-B-3.3). The 200 success body is
# the discriminated union over these; the model selects one via its ``kind`` discriminator.
_AGENT_RESULT_MODELS: tuple[type[BaseModel], ...] = (
    RecommendationList,
    PreferenceSummary,
    ComparativeAnalysis,
    BoundedResponse,
    RefusalResponse,
)

# JSON Schema ``$ref`` prefix Pydantic emits for nested definitions; we rewrite it to the
# OpenAPI components location.
_DEFS_PREFIX = "#/$defs/"
_COMPONENTS_PREFIX = "#/components/schemas/"


def _rewrite_refs(node: Any) -> Any:
    """Recursively rewrite Pydantic ``#/$defs/..`` refs to ``#/components/schemas/..``."""
    if isinstance(node, dict):
        return {
            key: (
                value.replace(_DEFS_PREFIX, _COMPONENTS_PREFIX)
                if key == "$ref" and isinstance(value, str)
                else _rewrite_refs(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


def _collect_schemas(models: tuple[type[BaseModel], ...]) -> dict[str, Any]:
    """Build the ``components.schemas`` map from the models + their nested ``$defs``.

    Each model's ``model_json_schema()`` is split into its own top-level schema (keyed by
    the model's title) plus any ``$defs`` it carries (shared nested models), with every
    ``$ref`` rewritten to the OpenAPI components location.
    """
    schemas: dict[str, Any] = {}
    for model in models:
        raw = model.model_json_schema(ref_template=f"{_COMPONENTS_PREFIX}{{model}}")
        defs = raw.pop("$defs", {})
        for name, definition in defs.items():
            schemas.setdefault(name, _rewrite_refs(definition))
        schemas[model.__name__] = _rewrite_refs(raw)
    return schemas


def build_openapi() -> dict[str, Any]:
    """Assemble the OpenAPI 3.1 document for POST /query from the Pydantic models."""
    models: tuple[type[BaseModel], ...] = (
        QueryRequest,
        ValidationErrorResponse,
        SubmitResponse,
        JobStatusResponse,
        JobProgress,
        WorkerError,
        NotFoundResponse,
        *_AGENT_RESULT_MODELS,
    )
    schemas = _collect_schemas(models)

    agent_result_one_of = [
        {"$ref": f"{_COMPONENTS_PREFIX}{model.__name__}"} for model in _AGENT_RESULT_MODELS
    ]

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "MovieIntel Serving API",
            "version": "0.1.0",
            "description": (
                "Agentic movie-intelligence query endpoint. POST a natural-language query; "
                "the service runs the Bedrock Converse agent loop over the enriched-movie "
                "tools and returns one structured AgentResult."
            ),
        },
        "paths": {
            "/query": {
                "post": {
                    "operationId": "postQuery",
                    "summary": "Run an agentic movie-intelligence query.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"{_COMPONENTS_PREFIX}QueryRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "A structured AgentResult (one of recommendations, "
                                "preferences, comparison, bounded, or refusal), "
                                "discriminated by 'kind'. A Guardrail refusal is a valid "
                                "200 outcome."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "oneOf": agent_result_one_of,
                                        "discriminator": {"propertyName": "kind"},
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": (
                                "The request body was malformed or violated the schema; the "
                                "agent was not invoked."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": f"{_COMPONENTS_PREFIX}ValidationErrorResponse"
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/jobs": {
                "post": {
                    "operationId": "submitJob",
                    "summary": "Submit an async movie-intelligence job.",
                    "description": (
                        "Enqueue the agentic query for background processing and return "
                        "immediately with a job id and poll URL; the agent runs on a worker "
                        "and the result is retrieved via GET /jobs/{id}."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"{_COMPONENTS_PREFIX}QueryRequest"}
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": (
                                "The job was accepted and queued; poll the returned poll_url "
                                "for progress and the final result."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"{_COMPONENTS_PREFIX}SubmitResponse"}
                                }
                            },
                        },
                        "400": {
                            "description": (
                                "The request body was malformed or violated the schema; no "
                                "job was created."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": f"{_COMPONENTS_PREFIX}ValidationErrorResponse"
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/jobs/{id}": {
                "get": {
                    "operationId": "getJob",
                    "summary": "Poll the status and result of an async job.",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "description": "The job id returned by POST /jobs.",
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": (
                                "The current job status with real per-phase progress; once "
                                "terminal, a succeeded job carries the AgentResult under "
                                "'result' and a failed job carries the error under 'error'."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"{_COMPONENTS_PREFIX}JobStatusResponse"}
                                }
                            },
                        },
                        "404": {
                            "description": "No job exists with that id (unknown or expired).",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"{_COMPONENTS_PREFIX}NotFoundResponse"}
                                }
                            },
                        },
                    },
                }
            },
        },
        "components": {"schemas": schemas},
    }


def render_openapi_yaml() -> str:
    """Render the OpenAPI document as JSON-compatible YAML (parseable by stdlib ``json``).

    JSON is a strict subset of YAML 1.2, so emitting indented JSON produces a valid YAML
    document without a third-party YAML dependency. A trailing newline is appended for a
    clean on-disk artifact.
    """
    return json.dumps(build_openapi(), indent=2, sort_keys=False) + "\n"


def write_openapi_yaml(path: str | None = None) -> str:
    """Write the rendered document to ``path`` (default :data:`OPENAPI_PATH`); return it."""
    target = Path(path) if path is not None else Path(OPENAPI_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_openapi_yaml()
    target.write_text(rendered, encoding="utf-8")
    return rendered


if __name__ == "__main__":  # pragma: no cover - CLI regeneration entrypoint
    written_to = write_openapi_yaml()
    print(f"wrote {OPENAPI_PATH}")  # noqa: T201
