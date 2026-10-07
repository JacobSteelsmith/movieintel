"""DynamoDB single-table repository for enriched movies (design §3.4, §7.3; REQ-A-5).

Implements the design access patterns against the ``MovieIntel`` table. The table/GSI
names come from :class:`PersistenceConfig` (no inline literals). A boto3 DynamoDB
``Table`` resource is injected (not created here): the real table is provisioned by CDK
in Task 6 with on-demand (``PAY_PER_REQUEST``) billing, and tests inject a moto table with
the same key schema.

Billing note: this code never sets or reads provisioned throughput; on-demand is the
intended billing mode (REQ-A-5.2, REQ-X-5.1), a table property set in Task 6.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr, Key

from movieintel.domain.schemas import Sentiment
from movieintel.persistence.config import PersistenceConfig
from movieintel.persistence.item import EnrichedMovie
from movieintel.persistence.keys import meta_sk, movie_pk, sentiment_gsi1pk

# BatchGetItem accepts at most 100 keys per request (DynamoDB hard limit); batch_get
# chunks by this even though the dataset is 50-100 items.
_BATCH_GET_LIMIT = 100


class MovieIntelRepository:
    """Repository over the single ``MovieIntel`` table (design §3.4).

    Constructed with an injected DynamoDB ``Table`` resource and a
    :class:`PersistenceConfig`. All reads/writes target that one table and its GSI1.
    """

    def __init__(self, *, table: Any, config: PersistenceConfig) -> None:
        self._table = table
        self._config = config

    def put(self, record: EnrichedMovie) -> None:
        """Idempotent upsert: unconditional ``PutItem`` keyed by movieid (REQ-A-5.5).

        PutItem overwrites any existing item with the same PK/SK, so re-running the
        pipeline replaces the record rather than creating a duplicate (last-write-wins).
        """
        self._table.put_item(Item=record.to_item())

    def get(self, movie_id: int | str) -> EnrichedMovie | None:
        """Get one enriched movie by id (access pattern 1): ``GetItem`` PK/SK.

        Returns ``None`` when no item exists for the id.
        """
        response = self._table.get_item(Key={"PK": movie_pk(movie_id), "SK": meta_sk()})
        item = response.get("Item")
        return None if item is None else EnrichedMovie.from_item(item)

    def batch_get(self, movie_ids: Sequence[int | str]) -> list[EnrichedMovie]:
        """Batch get N movies by id (access pattern 2, powers ``compare_movies``).

        Uses ``BatchGetItem``, chunking at the 100-key limit and retrying
        ``UnprocessedKeys`` so the call is correct even if DynamoDB returns a partial
        batch. Result order is not guaranteed (DynamoDB does not order batch results).
        """
        if not movie_ids:
            return []

        resource = self._table.meta.client
        table_name = self._table.name
        records: list[EnrichedMovie] = []

        for start in range(0, len(movie_ids), _BATCH_GET_LIMIT):
            chunk = movie_ids[start : start + _BATCH_GET_LIMIT]
            keys = [{"PK": movie_pk(mid), "SK": meta_sk()} for mid in chunk]
            request = {table_name: {"Keys": keys}}
            while request:
                response = resource.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(table_name, []):
                    records.append(EnrichedMovie.from_item(item))
                # Retry any keys DynamoDB could not serve this round.
                request = response.get("UnprocessedKeys") or {}

        return records

    def query_by_sentiment(
        self, sentiment: Sentiment, *, limit: int | None = None
    ) -> list[EnrichedMovie]:
        """Filter by sentiment, ranked by PES descending (access pattern 3): ``Query GSI1``.

        ``ScanIndexForward=False`` walks GSI1SK (zero-padded PES) in descending order, so
        the highest-PES movie in the sentiment partition comes first. Because PES is stored
        zero-padded, this is a true numeric ranking, not a lexical one.
        """
        kwargs: dict[str, Any] = {
            "IndexName": self._config.gsi1_name,
            "KeyConditionExpression": Key("GSI1PK").eq(sentiment_gsi1pk(sentiment)),
            "ScanIndexForward": False,
        }
        if limit is not None:
            kwargs["Limit"] = limit
        response = self._table.query(**kwargs)
        return [EnrichedMovie.from_item(item) for item in response.get("Items", [])]

    def scan_numeric_range(
        self,
        *,
        min_budget: float | None = None,
        max_budget: float | None = None,
        min_revenue: float | None = None,
        max_revenue: float | None = None,
        min_runtime: float | None = None,
        max_runtime: float | None = None,
    ) -> list[EnrichedMovie]:
        """Sentiment-agnostic numeric-range filter (access pattern 4): small-N ``Scan``.

        A filtered ``Scan`` is used deliberately. The dataset is 50-100 items (design
        §7.3), so scanning the whole table once and filtering on budget/revenue/runtime is
        cheaper and simpler than maintaining a GSI per numeric field. This is the documented
        small-N tradeoff; it would NOT be acceptable at large scale, where a GSI or a
        different store would be required.
        """
        filters = _numeric_range_filters(
            min_budget=min_budget,
            max_budget=max_budget,
            min_revenue=min_revenue,
            max_revenue=max_revenue,
            min_runtime=min_runtime,
            max_runtime=max_runtime,
        )

        scan_kwargs: dict[str, Any] = {}
        if filters is not None:
            scan_kwargs["FilterExpression"] = filters

        records: list[EnrichedMovie] = []
        response = self._table.scan(**scan_kwargs)
        while True:
            for item in response.get("Items", []):
                records.append(EnrichedMovie.from_item(item))
            last_key = response.get("LastEvaluatedKey")
            if last_key is None:
                break
            response = self._table.scan(ExclusiveStartKey=last_key, **scan_kwargs)
        return records


def _numeric_range_filters(
    *,
    min_budget: float | None,
    max_budget: float | None,
    min_revenue: float | None,
    max_revenue: float | None,
    min_runtime: float | None,
    max_runtime: float | None,
) -> Any | None:
    """Build the combined ``FilterExpression`` for the numeric-range scan, or ``None``.

    Each supplied bound narrows the stored attribute; absent bounds impose no constraint.
    Returns ``None`` when no bounds are given (an unfiltered full scan).
    """
    conditions: list[Any] = []
    for attribute, lo, hi in (
        ("budget", min_budget, max_budget),
        ("revenue", min_revenue, max_revenue),
        ("runtime", min_runtime, max_runtime),
    ):
        # Bounds are compared against stored numbers; floats must be Decimal for DynamoDB.
        if lo is not None:
            conditions.append(Attr(attribute).gte(_as_number(lo)))
        if hi is not None:
            conditions.append(Attr(attribute).lte(_as_number(hi)))

    if not conditions:
        return None
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = combined & condition
    return combined


def _as_number(value: float) -> int | Decimal:
    """Coerce a numeric bound to a DynamoDB-safe type (ints stay int; floats -> Decimal)."""
    if isinstance(value, int):
        return value
    return Decimal(str(value))
