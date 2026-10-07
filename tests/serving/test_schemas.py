"""Serving request/error schema tests (Task 10 FEAT-001, REQ-B-4.1/.4).

The serving layer exposes a strict :class:`QueryRequest` for the POST ``/query`` body and a
structured :class:`ValidationErrorResponse` for a schema-violating body. Both are strict
(``extra="forbid"``) like every other model in the package, and the error model mirrors
``agent/response.py`` ``FinalAnswerError`` (a ``Literal`` discriminator + a ``detail`` list
built from ``ValidationError.errors(include_url=False)``). These tests pin accept/reject
behavior before the models exist (test-first).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movieintel.serving.schemas import (
    MAX_TURNS_CAP,
    QueryRequest,
    ValidationErrorResponse,
)

# --------------------------------------------------------------------------- accept


def test_query_request_accepts_minimal_body_without_max_turns() -> None:
    req = QueryRequest.model_validate({"query": "Recommend action movies."})
    assert req.query == "Recommend action movies."
    assert req.max_turns is None


def test_query_request_accepts_body_with_max_turns() -> None:
    req = QueryRequest.model_validate({"query": "Compare two films.", "max_turns": 4})
    assert req.query == "Compare two films."
    assert req.max_turns == 4


def test_query_request_accepts_max_turns_at_cap() -> None:
    req = QueryRequest.model_validate({"query": "ok", "max_turns": MAX_TURNS_CAP})
    assert req.max_turns == MAX_TURNS_CAP


# --------------------------------------------------------------------------- reject


def test_query_request_rejects_missing_query() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({})


def test_query_request_rejects_empty_query() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": ""})


def test_query_request_rejects_non_string_query() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": 123})


def test_query_request_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "ok", "unexpected": "x"})


def test_query_request_rejects_max_turns_below_floor() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "ok", "max_turns": 0})


def test_query_request_rejects_max_turns_above_cap() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "ok", "max_turns": MAX_TURNS_CAP + 1})


# ----------------------------------------------------------- ValidationErrorResponse


def test_validation_error_response_from_validation_error() -> None:
    try:
        QueryRequest.model_validate({})
    except ValidationError as exc:
        detail = exc.errors(include_url=False)
        response = ValidationErrorResponse(detail=detail)
    assert response.error == "invalid_request"
    assert response.detail == detail
    assert response.detail  # at least one error entry present


def test_validation_error_response_is_strict() -> None:
    with pytest.raises(ValidationError):
        ValidationErrorResponse.model_validate(
            {"error": "invalid_request", "detail": [], "extra": 1}
        )


def test_validation_error_response_serializes_json_safe() -> None:
    try:
        QueryRequest.model_validate({"query": ""})
    except ValidationError as exc:
        response = ValidationErrorResponse(detail=exc.errors(include_url=False))
    dumped = response.model_dump(mode="json")
    assert dumped["error"] == "invalid_request"
    assert isinstance(dumped["detail"], list)
