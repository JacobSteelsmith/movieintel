"""Final structured-output models for the agent loop (Task 9, design §3.7, REQ-B-3.3).

The loop's final answer is a VALIDATED Pydantic object (REQ-X-1), one of three result
shapes the model selects via a dedicated ``final_answer`` tool: a recommendation list, a
preference summary, or a comparative analysis. Two further outcomes carry no model content
— :class:`BoundedResponse` (the MAX_TURNS bound, REQ-B-3.2) and :class:`RefusalResponse`
(a Guardrail intervention, REQ-B-3.5). ``AgentResult`` is the discriminated union the loop
returns, mirroring ``enrichment.results.EnrichmentResult``.

All models are strict (``extra="forbid"``) like every other model in the package, reuse the
domain enums (:class:`Sentiment`, :class:`Mood`) rather than redefining them, and carry a
literal ``kind`` discriminator. ``FINAL_ANSWER_SPEC`` is the Converse ``toolSpec`` the loop
appends to ``ALL_TOOL_SPECS``; it lives here, co-located with the models it mirrors, and is
NOT added to the Task 8 ``ALL_TOOL_SPECS`` (which is frozen by the Task 8 contract tests).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from movieintel.domain.schemas import Mood, Sentiment


class RecommendedMovie(BaseModel):
    """One movie in a :class:`RecommendationList` (the model's chosen rationale + facts)."""

    model_config = ConfigDict(extra="forbid")

    movie_id: str
    title: str
    sentiment: Sentiment
    pes: float
    mood: Mood
    rationale: str


class RecommendationList(BaseModel):
    """Recommendation result — e.g. "Recommend action movies ..." (REQ-B-3.3)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["recommendations"] = "recommendations"
    query_summary: str
    movies: list[RecommendedMovie]


class PreferenceSummary(BaseModel):
    """Preference-summary result — e.g. "Summarize preferences for a user ..." (REQ-B-3.3)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["preferences"] = "preferences"
    subject: str
    summary: str
    highlights: list[str]


class ComparativeAnalysis(BaseModel):
    """Comparative-analysis result for a compare-oriented task (REQ-B-3.3)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["comparison"] = "comparison"
    subjects: list[str]
    dimensions: list[str]
    narrative: str


class BoundedResponse(BaseModel):
    """The MAX_TURNS outcome: the loop stopped at its bound without a final answer (REQ-B-3.2)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bounded"] = "bounded"
    reason: str
    turns_used: int


class RefusalResponse(BaseModel):
    """The Guardrail-intervention outcome; carries NO movie/content data (REQ-B-3.5)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["refusal"] = "refusal"
    reason: str


class UnknownToolError(BaseModel):
    """Structured error for a tool name the dispatcher does not know (surfaced, not raised)."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    error: Literal["unknown_tool"] = "unknown_tool"


class FinalAnswerError(BaseModel):
    """Structured error when the model's ``final_answer`` payload fails validation (REQ-X-1)."""

    model_config = ConfigDict(extra="forbid")

    error: Literal["invalid_final_answer"] = "invalid_final_answer"
    detail: list[dict[str, Any]]


# The agent result is exactly one of these five outcomes (discriminated on ``kind``),
# mirroring ``enrichment.results.EnrichmentResult``.
type AgentResult = (
    RecommendationList | PreferenceSummary | ComparativeAnalysis | BoundedResponse | RefusalResponse
)

# The three terminal result models the model may select via ``final_answer``, keyed by the
# ``kind`` discriminator the model sends.
_FINAL_ANSWER_MODELS: dict[str, type[BaseModel]] = {
    "recommendations": RecommendationList,
    "preferences": PreferenceSummary,
    "comparison": ComparativeAnalysis,
}


def parse_final_answer(payload: dict[str, Any]) -> AgentResult | FinalAnswerError:
    """Validate a ``final_answer`` payload into one of the three terminal result models.

    Selects the model by the payload's ``kind`` and validates with Pydantic so the loop
    never returns an unvalidated object (REQ-X-1). On an unknown/missing ``kind`` or a
    validation failure, returns a :class:`FinalAnswerError` that the loop surfaces back to
    the model as an error toolResult so it can correct (it never raises).
    """
    kind = payload.get("kind")
    model = _FINAL_ANSWER_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        return FinalAnswerError(
            detail=[
                {
                    "loc": ["kind"],
                    "msg": f"unknown or missing final-answer kind: {kind!r}",
                    "type": "value_error",
                }
            ]
        )
    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        return FinalAnswerError(detail=exc.errors(include_url=False))
    # The three terminal models are all members of AgentResult.
    return validated  # type: ignore[return-value]


def bounded_response(turns_used: int) -> BoundedResponse:
    """Construct the MAX_TURNS bounded outcome (REQ-B-3.2), parallel to ``Failed``."""
    return BoundedResponse(
        reason=(
            f"The agent reached its turn bound ({turns_used}) without producing a final answer."
        ),
        turns_used=turns_used,
    )


def safe_refusal() -> RefusalResponse:
    """Construct the safe Guardrail-intervention refusal (REQ-B-3.5), parallel to ``Blocked``."""
    return RefusalResponse(
        reason="The request was blocked by the Bedrock Guardrail; no content was produced."
    )


# The Converse ``toolSpec`` for the model's final answer. Its ``inputSchema`` lists the
# ``kind`` discriminator plus the union of per-kind fields; the loop validates the model's
# ``input`` against the matching terminal model via :func:`parse_final_answer`. This spec is
# composed onto ``ALL_TOOL_SPECS`` by the loop and is NOT part of the Task 8 specs module.
FINAL_ANSWER_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "final_answer",
        "description": (
            "Return the final structured answer to the user. Choose exactly one 'kind': "
            "'recommendations' (a ranked movie list), 'preferences' (a user-preference "
            "summary), or 'comparison' (a side-by-side comparative analysis). Provide only "
            "the fields for the chosen kind."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["recommendations", "preferences", "comparison"],
                    },
                    "query_summary": {"type": "string"},
                    "movies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "movie_id": {"type": "string"},
                                "title": {"type": "string"},
                                "sentiment": {
                                    "type": "string",
                                    "enum": ["positive", "negative", "neutral"],
                                },
                                "pes": {"type": "number"},
                                "mood": {
                                    "type": "string",
                                    "enum": [
                                        "dark",
                                        "light",
                                        "intense",
                                        "uplifting",
                                        "tense",
                                        "lighthearted",
                                    ],
                                },
                                "rationale": {"type": "string"},
                            },
                            "required": [
                                "movie_id",
                                "title",
                                "sentiment",
                                "pes",
                                "mood",
                                "rationale",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "subject": {"type": "string"},
                    "summary": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                    "subjects": {"type": "array", "items": {"type": "string"}},
                    "dimensions": {"type": "array", "items": {"type": "string"}},
                    "narrative": {"type": "string"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            }
        },
    }
}
