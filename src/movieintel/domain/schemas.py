"""Pydantic schemas and enums for the five enrichment attributes (REQ-A-2, REQ-X-1).

Task 2 only *defines* these types and computes the deterministic
``EffectivenessScore.value``/flags (see ``pes.py``). The LLM-assigned fields
(``tier``, ``explanation``, ``overview_sentiment``, the tier labels, ``mood``,
``reasoning_basis``) are typed here but produced in Task 3. The models are strict
(``extra="forbid"``) so malformed/partial payloads reject (REQ-X-1.3).

Field names mirror design §3.2 exactly. ``EffectivenessScore`` carries one
deterministic extension beyond §3.2: ``rating_defined`` (the honest-missing flag
for a missing rating), the counterpart of ``roi_defined`` for the budget branch.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Sentiment(StrEnum):
    """Overview sentiment label (LLM-assigned in Task 3)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class Tier(StrEnum):
    """Budget/revenue tier label (LLM-assigned in Task 3)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PESTier(StrEnum):
    """Production-effectiveness tier label derived from the PES value (Task 3)."""

    UNDERPERFORMER = "underperformer"
    SOLID = "solid"
    STANDOUT = "standout"


class Mood(StrEnum):
    """Fixed mood/tone label set; the model must choose one best-fit member (§7.1).

    Exactly these six members — out-of-set values are rejected.
    """

    DARK = "dark"
    LIGHT = "light"
    INTENSE = "intense"
    UPLIFTING = "uplifting"
    TENSE = "tense"
    LIGHTHEARTED = "lighthearted"


# A tier that may be the explicit sentinel "unknown" when the source field is
# missing (REQ-A-2.4 — nothing is fabricated).
TierOrUnknown = Tier | Literal["unknown"]


class EffectivenessScore(BaseModel):
    """Production Effectiveness Score result (design §3.2).

    ``value``, ``roi_defined`` and ``rating_defined`` are deterministic and set
    by the Task 2 PES function. ``tier`` and ``explanation`` are LLM-assigned in
    Task 3, so they default to ``None``/``""`` here and are left unset by the
    deterministic function (Task 2 must not fabricate them).
    """

    model_config = ConfigDict(extra="forbid")

    value: float
    roi_defined: bool
    rating_defined: bool
    tier: PESTier | None = None
    explanation: str = ""


class EnrichmentAttributes(BaseModel):
    """The five enrichment attributes with design §3.2 field names (REQ-A-2.1)."""

    model_config = ConfigDict(extra="forbid")

    overview_sentiment: Sentiment
    budget_tier: TierOrUnknown
    revenue_tier: TierOrUnknown
    effectiveness: EffectivenessScore
    mood: Mood
    reasoning_basis: str
