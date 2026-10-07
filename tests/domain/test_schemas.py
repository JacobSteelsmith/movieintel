"""Accept/reject tests for the enrichment schemas (REQ-X-1.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
    Tier,
)


def _valid_attributes_payload() -> dict[str, object]:
    return {
        "overview_sentiment": "positive",
        "budget_tier": "high",
        "revenue_tier": "unknown",
        "effectiveness": {
            "value": 84.85,
            "tier": "standout",
            "explanation": "Strong ROI and audience rating.",
            "roi_defined": True,
            "rating_defined": True,
        },
        "mood": "intense",
        "reasoning_basis": "Budget and revenue both well above the sampled median.",
    }


def test_mood_accepts_every_member() -> None:
    for member in ("dark", "light", "intense", "uplifting", "tense", "lighthearted"):
        assert Mood(member).value == member


def test_mood_has_exactly_six_members() -> None:
    assert {m.value for m in Mood} == {
        "dark",
        "light",
        "intense",
        "uplifting",
        "tense",
        "lighthearted",
    }


def test_mood_rejects_out_of_set_value() -> None:
    with pytest.raises(ValueError):
        Mood("scary")


def test_enrichment_rejects_out_of_set_mood() -> None:
    payload = _valid_attributes_payload()
    payload["mood"] = "scary"
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)


def test_tier_members_and_unknown_accepted() -> None:
    for tier in ("low", "medium", "high", "unknown"):
        payload = _valid_attributes_payload()
        payload["budget_tier"] = tier
        payload["revenue_tier"] = tier
        attrs = EnrichmentAttributes(**payload)
        assert attrs.budget_tier == (Tier(tier) if tier != "unknown" else "unknown")


def test_tier_rejects_arbitrary_string() -> None:
    payload = _valid_attributes_payload()
    payload["budget_tier"] = "gigantic"
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)


def test_sentiment_members_and_rejection() -> None:
    for sentiment in ("positive", "negative", "neutral"):
        assert Sentiment(sentiment).value == sentiment
    with pytest.raises(ValueError):
        Sentiment("mixed")


def test_pestier_members_and_rejection() -> None:
    for tier in ("underperformer", "solid", "standout"):
        assert PESTier(tier).value == tier
    with pytest.raises(ValueError):
        PESTier("flop")


def test_effectiveness_accepts_deterministic_only_payload() -> None:
    score = EffectivenessScore(value=80.0, roi_defined=False, rating_defined=True)
    assert score.value == 80.0
    assert score.tier is None
    assert score.explanation == ""


def test_effectiveness_rejects_non_float_value() -> None:
    with pytest.raises(ValidationError):
        EffectivenessScore(value="lots", roi_defined=True, rating_defined=True)


def test_effectiveness_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        EffectivenessScore(
            value=80.0,
            roi_defined=True,
            rating_defined=True,
            surprise="nope",  # type: ignore[call-arg]
        )


def test_enrichment_accepts_full_payload() -> None:
    attrs = EnrichmentAttributes(**_valid_attributes_payload())
    assert attrs.overview_sentiment is Sentiment.POSITIVE
    assert attrs.mood is Mood.INTENSE
    assert attrs.effectiveness.value == 84.85


def test_enrichment_rejects_partial_payload() -> None:
    payload = _valid_attributes_payload()
    del payload["mood"]
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)

    payload = _valid_attributes_payload()
    del payload["overview_sentiment"]
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)


def test_enrichment_rejects_malformed_effectiveness() -> None:
    payload = _valid_attributes_payload()
    payload["effectiveness"] = "not-a-model"
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)


def test_enrichment_rejects_extra_field() -> None:
    payload = _valid_attributes_payload()
    payload["unexpected"] = "value"
    with pytest.raises(ValidationError):
        EnrichmentAttributes(**payload)
