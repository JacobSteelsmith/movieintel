"""The evaluation harness entry point (REQ-X-2).

``evaluate`` composes the five metric functions into a single :class:`EvalReport`
deterministically — no I/O, no Bedrock. Determinism comes from the inputs: given the
same records and the same judge, it always returns an equal report (REQ-X-2.3). The
sentiment judge is injected (deterministic stub in CI, Bedrock on the live path).
"""

from __future__ import annotations

from collections.abc import Sequence

from movieintel.eval.metrics import (
    guardrail_coverage,
    mood_agreement,
    pes_exactness,
    schema_validation_rate,
    sentiment_agreement,
)
from movieintel.eval.models import EvalRecord, SentimentJudge
from movieintel.eval.report import EvalReport


def evaluate(records: Sequence[EvalRecord], *, judge: SentimentJudge) -> EvalReport:
    """Compute all five golden-set metrics and return the structured report.

    Guardrail coverage and PES exactness are measured over every record; the label
    metrics (sentiment, mood) are measured over schema-valid records only. Mood uses a
    deterministic direct label-match; only sentiment consults the injected ``judge``.

    :raises ValueError: when ``records`` is empty. An empty evaluation is a usage/
        loading error, not a passing run — failing closed here prevents a vacuous
        "100% pass" report from masking a fixture-loading bug.
    """
    if not records:
        raise ValueError("evaluate() requires at least one EvalRecord; got an empty sample.")
    return EvalReport(
        schema_validation=schema_validation_rate(records),
        pes_exactness=pes_exactness(records),
        sentiment_agreement=sentiment_agreement(records, judge),
        mood_agreement=mood_agreement(records),
        guardrail_coverage=guardrail_coverage(records),
    )
