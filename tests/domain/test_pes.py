"""PES exactness, branch, clamp, and reproducibility tests (REQ-A-3, REQ-X-2.5).

Expected PES values are hand-computed (arithmetic shown inline) and cross-checked
against ``tests/domain/_reference_pes.py``, an independent reference implementation.
They are NEVER produced by calling the production PES function (REQ-X-2.5).
"""

from __future__ import annotations

from movieintel.data_access.models import MovieRow, RatingScale, SampledMovie
from movieintel.domain.pes import (
    W_RATING,
    W_ROI,
    production_effectiveness_score,
    score_sampled_movie,
)
from movieintel.domain.schemas import EffectivenessScore

from ._reference_pes import reference_pes_value


def test_full_branch_exact_value() -> None:
    # roi = (3_000_000 - 1_000_000) / 1_000_000 = 2.0
    # logistic(2.0) = 0.8807970779778823 -> roi_norm (clamped) = 0.8807970779778823
    # rating_norm = 4.0 / 5.0 = 0.8
    # value = round(100 * (0.6*0.8807970779778823 + 0.4*0.8), 2) = round(84.84782..., 2) = 84.85
    score = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert score.value == 84.85
    assert score.roi_defined is True
    assert score.rating_defined is True
    assert score.value == reference_pes_value(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )


def test_reproducible_identical_inputs() -> None:
    first = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    second = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert first.value == second.value
    assert first == second


def test_budget_zero_rating_only_branch() -> None:
    # rating-only: value = round(100 * 0.8, 2) = 80.0
    score = production_effectiveness_score(
        budget=0, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert score.value == 80.0
    assert score.roi_defined is False
    assert score.rating_defined is True
    assert score.value == reference_pes_value(
        budget=0, revenue=3_000_000, rating=4.0, effective_max=5.0
    )


def test_budget_none_rating_only_branch() -> None:
    score = production_effectiveness_score(
        budget=None, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert score.value == 80.0
    assert score.roi_defined is False
    assert score.rating_defined is True


def test_budget_negative_rating_only_branch() -> None:
    score = production_effectiveness_score(
        budget=-5, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert score.value == 80.0
    assert score.roi_defined is False


def test_rating_missing_branch() -> None:
    # rating_norm = 0.0; value = round(100 * (0.6*0.8807970779778823 + 0.4*0), 2) = 52.85
    score = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=None, effective_max=5.0
    )
    assert score.value == 52.85
    assert score.roi_defined is True
    assert score.rating_defined is False
    assert score.value == reference_pes_value(
        budget=1_000_000, revenue=3_000_000, rating=None, effective_max=5.0
    )


def test_no_evidence_combined_edge() -> None:
    # budget None and rating None -> value 0.0, both flags False
    score = production_effectiveness_score(
        budget=None, revenue=None, rating=None, effective_max=5.0
    )
    assert score.value == 0.0
    assert score.roi_defined is False
    assert score.rating_defined is False


def test_rating_at_scale_max_clamps_to_one() -> None:
    # rating == effective_max -> rating_norm == 1.0; rating-only branch -> value 100.0
    score = production_effectiveness_score(budget=0, revenue=0, rating=5.0, effective_max=5.0)
    assert score.value == 100.0


def test_rating_above_scale_is_clamped() -> None:
    # dirty input rating > effective_max -> rating_norm clamped to 1.0 -> value 100.0
    score = production_effectiveness_score(budget=0, revenue=0, rating=7.5, effective_max=5.0)
    assert score.value == 100.0


def test_very_large_roi_stays_in_bounds() -> None:
    # roi huge -> logistic -> ~1.0 -> roi_norm clamped to 1.0; rating at max -> value 100.0
    score = production_effectiveness_score(
        budget=1, revenue=1_000_000_000, rating=5.0, effective_max=5.0
    )
    assert score.value == 100.0
    assert score.value <= 100.0


def test_very_negative_roi_stays_non_negative() -> None:
    # roi = (0 - 1_000_000)/1_000_000 = -1.0; logistic(-1.0) = 0.2689414213699951
    # value = round(100 * (0.6*0.2689414213699951 + 0.4*0.8), 2) = 48.14
    score = production_effectiveness_score(
        budget=1_000_000, revenue=0, rating=4.0, effective_max=5.0
    )
    assert score.value == 48.14
    assert score.roi_defined is True
    assert score.value == reference_pes_value(
        budget=1_000_000, revenue=0, rating=4.0, effective_max=5.0
    )


def test_weights_overridable() -> None:
    # w_roi=1.0, w_rating=0.0 -> value = round(100 * 1.0*0.8807970779778823, 2) = 88.08
    score = production_effectiveness_score(
        budget=1_000_000,
        revenue=3_000_000,
        rating=4.0,
        effective_max=5.0,
        w_roi=1.0,
        w_rating=0.0,
    )
    assert score.value == 88.08
    assert score.value == reference_pes_value(
        budget=1_000_000,
        revenue=3_000_000,
        rating=4.0,
        effective_max=5.0,
        w_roi=1.0,
        w_rating=0.0,
    )


def test_default_weights_match_spec() -> None:
    assert W_ROI == 0.6
    assert W_RATING == 0.4


def test_effective_max_is_honored_not_assumed() -> None:
    # same rating, different scales -> different rating_norm.
    # effective_max=10.0: rating_norm = 4.0/10.0 = 0.4
    # value = round(100 * (0.6*0.8807970779778823 + 0.4*0.4), 2) = 68.85
    scale_ten = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=10.0
    )
    scale_five = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert scale_ten.value == 68.85
    assert scale_ten.value != scale_five.value
    assert scale_ten.value == reference_pes_value(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=10.0
    )


def test_result_is_effectiveness_score_with_task3_defaults() -> None:
    score = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert isinstance(score, EffectivenessScore)
    assert score.tier is None
    assert score.explanation == ""


def test_score_sampled_movie_adapter_matches_scalar() -> None:
    movie = MovieRow(movie_id=1, title="X", budget=1_000_000, revenue=3_000_000)
    sampled = SampledMovie(movie=movie, rating=4.0, rating_count=10)
    scale = RatingScale(observed_min=0.5, observed_max=5.0, effective_max=5.0, fallback_used=False)
    adapter = score_sampled_movie(sampled, scale)
    scalar = production_effectiveness_score(
        budget=1_000_000, revenue=3_000_000, rating=4.0, effective_max=5.0
    )
    assert adapter == scalar
