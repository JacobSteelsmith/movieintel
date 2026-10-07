"""Key-encoding tests, incl. the zero-padding lexical-vs-numeric guard (design §7.3)."""

from __future__ import annotations

from movieintel.domain.schemas import Sentiment
from movieintel.persistence.keys import (
    meta_sk,
    movie_pk,
    pes_gsi1sk,
    sentiment_gsi1pk,
)


def test_movie_pk_and_meta_sk() -> None:
    assert movie_pk(42) == "MOVIE#42"
    assert movie_pk("42") == "MOVIE#42"
    assert meta_sk() == "META"


def test_sentiment_gsi1pk() -> None:
    assert sentiment_gsi1pk(Sentiment.POSITIVE) == "SENTIMENT#positive"
    assert sentiment_gsi1pk(Sentiment.NEGATIVE) == "SENTIMENT#negative"
    assert sentiment_gsi1pk(Sentiment.NEUTRAL) == "SENTIMENT#neutral"


def test_pes_gsi1sk_zero_pads_to_lossless_integer() -> None:
    assert pes_gsi1sk(0.0) == "PES#0000000"
    assert pes_gsi1sk(9.50) == "PES#0000950"
    assert pes_gsi1sk(20.0) == "PES#0002000"
    assert pes_gsi1sk(100.0) == "PES#0010000"


def test_zero_padding_is_load_bearing_for_numeric_sort() -> None:
    """Padded keys sort numerically; the raw unpadded strings would mis-sort lexically."""
    # Padded: 9.50 sorts BELOW 100.00 (correct numeric order).
    assert pes_gsi1sk(9.50) < pes_gsi1sk(100.0)
    # Unpadded would mis-sort: "PES#100.0" < "PES#9.5" lexically ('1' < '9').
    assert "PES#100.0" < "PES#9.5"
