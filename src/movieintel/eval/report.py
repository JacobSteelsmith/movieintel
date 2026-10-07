"""The metrics artifact: a typed report plus JSON/table serializers (REQ-X-2.4).

``EvalReport`` holds the five :class:`MetricResult`s as named fields. It is a Pydantic
model (project convention, ``extra="forbid"``) so ``model_dump()`` yields a
JSON-serializable summary, and ``render_table()`` renders a plain-text table — the
"typed result object AND serializable table/JSON" the task requires. ``assert_exact``
is the gate the eval-as-tests / pipeline uses to fail on sub-100% PES exactness.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from movieintel.eval.models import MetricResult

# Metric fields in a stable display order (header labels == field names).
_METRIC_ORDER = (
    "schema_validation",
    "pes_exactness",
    "sentiment_agreement",
    "mood_agreement",
    "guardrail_coverage",
)


class EvalReport(BaseModel):
    """Structured metrics artifact over the golden set (REQ-X-2.2, REQ-X-2.4)."""

    model_config = ConfigDict(extra="forbid")

    schema_validation: MetricResult
    pes_exactness: MetricResult
    sentiment_agreement: MetricResult
    mood_agreement: MetricResult
    guardrail_coverage: MetricResult

    def _metrics(self) -> list[MetricResult]:
        return [getattr(self, name) for name in _METRIC_ORDER]

    def all_passed(self) -> bool:
        """True when every metric gate passed."""
        return all(metric.passed for metric in self._metrics())

    def render_table(self) -> str:
        """Render a plain-text table with one row per metric (REQ-X-2.4)."""
        header = f"{'metric':<22} {'rate':>8}  {'passed':<6}  detail"
        rows = [header, "-" * len(header)]
        for metric in self._metrics():
            rows.append(
                f"{metric.name:<22} {metric.rate:>8.3f}  {str(metric.passed):<6}  {metric.detail}"
            )
        return "\n".join(rows)


def assert_exact(report: EvalReport) -> None:
    """Fail when deterministic-score exactness is below 100% (REQ-X-2.5).

    :raises AssertionError: when ``report.pes_exactness.passed`` is False.
    """
    if not report.pes_exactness.passed:
        raise AssertionError(
            "Deterministic PES exactness below 100%: "
            f"{report.pes_exactness.detail} (rate={report.pes_exactness.rate})"
        )
