"""Key-encoding helpers for the single-table design (design §3.4, §7.3).

The ``MovieIntel`` table overloads one physical key schema with typed prefixes
(``MOVIE#<id>``, ``SENTIMENT#<s>``) as the amazon-dynamodb skill prescribes for
single-table modeling. The GSI1 sort key encodes PES numerically.

Why zero-padding (amazon-dynamodb skill): DynamoDB sorts sort keys lexically (byte
order), so a raw numeric string mis-sorts — ``"100.0"`` sorts before ``"9.5"`` because
``'1' < '9'``. PES is ``0.00``–``100.00`` with two decimals (domain §3.2), so
``round(value * 100)`` is an integer in ``0..10000`` with no fractional loss; formatting
it as a fixed-width ``%07d`` makes GSI1SK sort numerically, which is what a
``ScanIndexForward=False`` query needs to rank by PES descending.
"""

from __future__ import annotations

from movieintel.domain.schemas import Sentiment

# Fixed sentinel for the single enriched-movie item under each movie partition.
META_SK = "META"

# Scale/width for the zero-padded PES sort key. PES is 0.00-100.00 (2 dp); scaling by 100
# yields an int in 0..10000 and 7 digits leaves headroom while sorting correctly.
_PES_SCALE = 100
_PES_WIDTH = 7


def movie_pk(movie_id: int | str) -> str:
    """Partition key for an enriched-movie item: ``MOVIE#<movieid>``."""
    return f"MOVIE#{movie_id}"


def meta_sk() -> str:
    """Sort key for the enriched-movie item: ``META``."""
    return META_SK


def sentiment_gsi1pk(sentiment: Sentiment) -> str:
    """GSI1 partition key grouping by sentiment: ``SENTIMENT#<sentiment>``."""
    return f"SENTIMENT#{sentiment.value}"


def pes_gsi1sk(value: float) -> str:
    """GSI1 sort key encoding PES as a zero-padded integer: ``PES#<padded>``.

    ``round(value * 100)`` converts the two-decimal PES to a lossless integer, then
    ``%07d`` pads it so lexical byte-order sorting matches numeric order (see module
    docstring). Example: ``9.50`` -> ``PES#0000950``; ``100.00`` -> ``PES#0010000``.
    """
    return f"PES#{round(value * _PES_SCALE):0{_PES_WIDTH}d}"
