"""Serving layer (Subsystem B, design §3.8/§3.10): HTTP contract over the agent loop.

This package exposes the agent loop (:func:`movieintel.agent.loop.run_agent`) behind an
HTTP request/response contract: a strict :class:`~movieintel.serving.schemas.QueryRequest`
for the POST ``/query`` body, a structured
:class:`~movieintel.serving.schemas.ValidationErrorResponse` for a schema-violating body,
and an OpenAPI 3.1 document generated from those Pydantic models (see
:mod:`movieintel.serving.openapi`). It composes the existing loop and the
``AgentResult`` union; it does NOT reimplement the loop, tools, repository, or config.
"""

from __future__ import annotations
