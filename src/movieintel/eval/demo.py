"""Task-4 demo entry point: an evaluation report table over the golden set (REQ-X-2.4).

Loads the committed golden fixtures, builds matching :class:`EvalRecord`s (produced labels
equal to the golden labels, computed PES equal to the independent expected literal) with
the deterministic stub judge, runs :func:`evaluate`, and prints both the rendered metrics
table and the JSON summary — the "produce an evaluation report table over the golden set"
demo criterion. Deterministic and Bedrock-free, mirroring CI.

Run with ``uv run movieintel-eval-demo`` (defaults to the committed golden set) or
``uv run movieintel-eval-demo --golden <path>``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from movieintel.domain.schemas import EnrichmentAttributes
from movieintel.eval.golden import load_golden
from movieintel.eval.harness import evaluate
from movieintel.eval.models import DeterministicSentimentJudge, EvalRecord, GoldenItem

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLDEN_PATH = _REPO_ROOT / "tests" / "fixtures" / "golden" / "golden_set.json"


def _record_from_golden(item: GoldenItem) -> EvalRecord:
    """A matching eval record for the demo: produced labels equal the golden labels."""
    produced = EnrichmentAttributes(
        overview_sentiment=item.expected_sentiment,
        budget_tier="medium",
        revenue_tier="high",
        effectiveness={
            "value": item.expected_pes,
            "roi_defined": item.budget is not None and item.budget > 0,
            "rating_defined": item.rating is not None,
        },
        mood=item.expected_mood,
        reasoning_basis="demo record matching the golden label",
    )
    return EvalRecord(
        movie_id=item.movie_id,
        produced=produced,
        raw_payload=None,
        computed_pes=item.expected_pes,
        expected_pes=item.expected_pes,
        expected_sentiment=item.expected_sentiment,
        expected_mood=item.expected_mood,
        guardrail_attached=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task-4 evaluation harness demo.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    items = load_golden(args.golden)
    records = [_record_from_golden(item) for item in items]
    report = evaluate(records, judge=DeterministicSentimentJudge())

    print(f"=== evaluation report over golden set (n={len(records)}) ===")
    print(report.render_table())
    print("\n=== metrics artifact (JSON) ===")
    print(json.dumps(report.model_dump(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
