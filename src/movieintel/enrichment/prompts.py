"""Pure prompt builders for the enrichment Converse call (design §3.3, §7.4).

All functions here are deterministic and I/O-free so they are directly unit-testable.
The structured-output strategy (design §7.4) is a strict JSON instruction plus the
embedded ``EnrichmentAttributes`` JSON Schema; the model is asked for a single JSON
object which the client then parses and Pydantic-validates.

The deterministic PES value is stated in the user message so the model assigns
``effectiveness.tier``/``explanation`` FROM it — the model never recomputes the value
(REQ-A-3.4). The client overwrites the deterministic fields regardless, so the prompt
framing and the code invariant agree.
"""

from __future__ import annotations

import json

from movieintel.data_access.models import SampledMovie
from movieintel.domain.schemas import EnrichmentAttributes

_SCHEMA_JSON = json.dumps(EnrichmentAttributes.model_json_schema(), indent=2, sort_keys=True)


def build_system_prompt() -> str:
    """Pin the model's role and the strict output contract (design §3.3)."""
    return (
        "You are a film-industry analyst that enriches movie records with structured "
        "attributes. Return ONLY one JSON object and nothing else: no prose, no markdown "
        "code fences, no explanation before or after the JSON. The JSON must conform to "
        "the provided schema. Include EVERY required field; never omit a field. Keep all "
        "string fields concise (roughly one short sentence) even when the source overview "
        "is long or dense. Choose enum values only from the allowed sets. Use the sentinel "
        '"unknown" for a budget or revenue tier when the underlying source value is '
        "missing - never fabricate a value. The Production Effectiveness Score value is "
        "computed deterministically and given to you; assign its tier and explanation FROM "
        "that value and do not alter the value itself."
    )


def _movie_facts(movie: SampledMovie, pes_value: float) -> dict[str, object]:
    """Collect the source facts the model reasons over (no fabrication)."""
    row = movie.movie
    return {
        "title": row.title,
        "overview": row.overview,
        "genres": row.genres,
        "language": row.language,
        "release_date": row.release_date,
        "budget": row.budget,
        "revenue": row.revenue,
        "runtime": row.runtime,
        "average_rating": movie.rating,
        "rating_count": movie.rating_count,
        "deterministic_pes_value": pes_value,
    }


def build_user_message(movie: SampledMovie, pes_value: float) -> str:
    """Ask for one JSON object matching the schema, stating the deterministic PES.

    ``pes_value`` is the already-computed deterministic PES; the model assigns the PES
    tier/explanation from it and must not recompute it (REQ-A-3.4).
    """
    facts = json.dumps(_movie_facts(movie, pes_value), indent=2, sort_keys=True)
    return (
        "Enrich the following movie. Return ONLY a single JSON object that validates "
        "against the schema below - no markdown fences, no prose. A long or dense overview "
        "must still yield concise, short string fields (for example reasoning_basis and "
        "effectiveness.explanation stay to roughly one sentence). Include every required "
        "field.\n\n"
        f"Movie facts:\n{facts}\n\n"
        f"The deterministic Production Effectiveness Score value is {pes_value}. Assign "
        "effectiveness.tier (underperformer | solid | standout) and a short "
        "effectiveness.explanation that references the contributing factors, based on "
        "this value. Echo this exact value in effectiveness.value.\n\n"
        f"JSON Schema for the required object:\n{_SCHEMA_JSON}"
    )


def build_repair_message(validation_error: str) -> str:
    """Echo the validation error and ask for a corrected single JSON object (REQ-A-4.3)."""
    return (
        "Your previous response did not produce a valid object. Fix the specific problem "
        "below and respond again.\n\n"
        f"Validation error:\n{validation_error}\n\n"
        "Return ONLY valid JSON for the schema - a single object, no prose, no markdown "
        "fences, every required field present and every string field concise."
    )
