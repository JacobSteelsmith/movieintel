"""Evaluation harness: golden set + five-metric eval-as-tests artifact (REQ-X-2).

Computes schema-validation rate, deterministic-PES exactness, LLM-as-judge sentiment
agreement, deterministic mood/tone direct-match agreement, and Guardrail coverage over a
committed golden set, emitting a typed, serializable metrics report. Public API
re-exported for ergonomic imports, mirroring the other layers.
"""

from __future__ import annotations

from movieintel.eval.golden import load_golden
from movieintel.eval.harness import evaluate
from movieintel.eval.models import (
    DeterministicSentimentJudge,
    EvalRecord,
    GoldenItem,
    MetricResult,
    SentimentJudge,
)
from movieintel.eval.report import EvalReport, assert_exact

__all__ = [
    "DeterministicSentimentJudge",
    "EvalRecord",
    "EvalReport",
    "GoldenItem",
    "MetricResult",
    "SentimentJudge",
    "assert_exact",
    "evaluate",
    "load_golden",
]
