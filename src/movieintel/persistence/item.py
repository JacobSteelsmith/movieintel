"""Enriched-movie record + DynamoDB item (de)serialization (design §3.4, §5.1).

``EnrichedMovie`` pairs the real Task 1 ``SampledMovie`` with the real Task 2
``EnrichmentAttributes`` — nothing is redefined. ``to_item``/``from_item`` convert
between the domain types and the single-table item dict so a round-trip is lossless by
construction (REQ-A-5.4): every field written is read back and reassembled into the exact
same ``SampledMovie`` + ``EnrichmentAttributes``.

Serialization subtlety: DynamoDB has no float type, so floats (``runtime``, ``rating``,
PES ``value``) are stored as ``Decimal`` and restored to ``float`` on read. Ints
(``budget``, ``revenue``) stay ints. Optional fields are omitted when ``None`` and
reconstructed as ``None`` when absent, so a missing value is never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from movieintel.data_access.models import MovieRow, SampledMovie
from movieintel.domain.schemas import (
    EffectivenessScore,
    EnrichmentAttributes,
    Mood,
    PESTier,
    Sentiment,
    Tier,
    TierOrUnknown,
)
from movieintel.persistence.keys import (
    meta_sk,
    movie_pk,
    pes_gsi1sk,
    sentiment_gsi1pk,
)


def _tier_to_str(tier: TierOrUnknown) -> str:
    """Serialize a tier-or-unknown to its string form (``Tier`` member or ``"unknown"``)."""
    return tier.value if isinstance(tier, Tier) else tier


def _str_to_tier(raw: str) -> TierOrUnknown:
    """Deserialize a stored tier string back to ``Tier`` or the ``"unknown"`` sentinel."""
    return "unknown" if raw == "unknown" else Tier(raw)


def _to_decimal(value: float) -> Decimal:
    """Convert a float to ``Decimal`` for DynamoDB (via ``str`` to avoid binary artifacts)."""
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class EnrichedMovie:
    """A source movie plus its five enrichment attributes, as persisted (design §5.1).

    Reuses the real domain types; this is the unit the repository stores and returns.
    """

    movie: SampledMovie
    attributes: EnrichmentAttributes

    @property
    def movie_id(self) -> str:
        """String form of the movie id — the key both stores agree on."""
        return str(self.movie.movie.movie_id)

    def to_item(self) -> dict[str, Any]:
        """Serialize to a single-table DynamoDB item (design §3.4 schema).

        Writes the key attributes (PK/SK/GSI1PK/GSI1SK), the serving/filter columns, and
        the five enrichment attributes + PES components. Floats become ``Decimal``;
        ``None`` optionals are omitted.
        """
        row = self.movie.movie
        attrs = self.attributes
        eff = attrs.effectiveness

        item: dict[str, Any] = {
            "PK": movie_pk(row.movie_id),
            "SK": meta_sk(),
            "GSI1PK": sentiment_gsi1pk(attrs.overview_sentiment),
            "GSI1SK": pes_gsi1sk(eff.value),
            "movieid": str(row.movie_id),
            "title": row.title,
            # Enrichment attributes (1-3, 5) + reasoning.
            "overview_sentiment": attrs.overview_sentiment.value,
            "budget_tier": _tier_to_str(attrs.budget_tier),
            "revenue_tier": _tier_to_str(attrs.revenue_tier),
            "mood": attrs.mood.value,
            "reasoning_basis": attrs.reasoning_basis,
            # PES components (attribute 4).
            "pes_value": _to_decimal(eff.value),
            "pes_roi_defined": eff.roi_defined,
            "pes_rating_defined": eff.rating_defined,
            "pes_explanation": eff.explanation,
        }

        # Optional serving/filter columns: omit when missing (never fabricate).
        _put_optional(item, "overview", row.overview)
        _put_optional(item, "genres", row.genres)
        _put_optional(item, "language", row.language)
        _put_optional(item, "releasedate", row.release_date)
        _put_optional(item, "imdb_id", row.imdb_id)
        _put_optional(item, "production_companies", row.production_companies)
        _put_optional(item, "status", row.status)
        _put_optional(item, "budget", row.budget)
        _put_optional(item, "revenue", row.revenue)
        _put_optional(item, "runtime", _optional_decimal(row.runtime))
        _put_optional(item, "rating", _optional_decimal(self.movie.rating))
        item["rating_count"] = self.movie.rating_count
        if eff.tier is not None:
            item["pes_tier"] = eff.tier.value
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> EnrichedMovie:
        """Reconstruct an ``EnrichedMovie`` from a stored item (inverse of :meth:`to_item`)."""
        movie_row = MovieRow(
            movie_id=int(item["movieid"]),
            title=item["title"],
            imdb_id=item.get("imdb_id"),
            overview=item.get("overview"),
            production_companies=item.get("production_companies"),
            release_date=item.get("releasedate"),
            budget=_optional_int(item.get("budget")),
            revenue=_optional_int(item.get("revenue")),
            runtime=_optional_float(item.get("runtime")),
            language=item.get("language"),
            genres=item.get("genres"),
            status=item.get("status"),
        )
        sampled = SampledMovie(
            movie=movie_row,
            rating=_optional_float(item.get("rating")),
            rating_count=int(item.get("rating_count", 0)),
        )

        pes_tier = item.get("pes_tier")
        effectiveness = EffectivenessScore(
            value=float(item["pes_value"]),
            roi_defined=bool(item["pes_roi_defined"]),
            rating_defined=bool(item["pes_rating_defined"]),
            tier=PESTier(pes_tier) if pes_tier is not None else None,
            explanation=item.get("pes_explanation", ""),
        )
        attributes = EnrichmentAttributes(
            overview_sentiment=Sentiment(item["overview_sentiment"]),
            budget_tier=_str_to_tier(item["budget_tier"]),
            revenue_tier=_str_to_tier(item["revenue_tier"]),
            effectiveness=effectiveness,
            mood=Mood(item["mood"]),
            reasoning_basis=item["reasoning_basis"],
        )
        return cls(movie=sampled, attributes=attributes)


def _put_optional(item: dict[str, Any], key: str, value: Any) -> None:
    """Set ``item[key] = value`` only when ``value`` is not ``None`` (omit missing)."""
    if value is not None:
        item[key] = value


def _optional_decimal(value: float | None) -> Decimal | None:
    """Convert an optional float to ``Decimal`` for DynamoDB, preserving ``None``."""
    return None if value is None else _to_decimal(value)


def _optional_int(value: Any) -> int | None:
    """Restore an optional stored number to ``int``, preserving ``None``."""
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    """Restore an optional stored number to ``float``, preserving ``None``."""
    return None if value is None else float(value)
