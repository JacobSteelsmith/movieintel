"""Opt-in live LLM-as-judge sentiment-agreement check (REQ-X-2.3, REQ-X-3.5).

Gated behind the ``live`` marker so it never runs in CI (run with ``-m live``). Skips
cleanly when no Guardrail id is configured, since every Converse call must attach one
(mirrors ``tests/enrichment/test_live.py``).
"""

from __future__ import annotations

import boto3
import pytest

from movieintel.config import BedrockConfig
from movieintel.domain.schemas import Sentiment
from movieintel.eval.judge import BedrockSentimentJudge


@pytest.mark.live
def test_live_sentiment_judge_runs() -> None:
    config = BedrockConfig.from_env()
    if config.guardrail_id is None:
        pytest.skip("No BEDROCK_GUARDRAIL_ID configured; skipping live eval judge.")

    client = boto3.client("bedrock-runtime", region_name=config.region)
    judge = BedrockSentimentJudge(client=client, config=config)

    verdict = judge.agree(Sentiment.POSITIVE, Sentiment.POSITIVE)
    assert isinstance(verdict, bool)
