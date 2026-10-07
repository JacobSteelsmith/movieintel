"""Live LLM-as-judge for sentiment agreement (design §3.5) — used ONLY on the live path.

``BedrockSentimentJudge`` implements the :class:`SentimentJudge` Protocol by asking a
Bedrock Converse model whether two sentiment labels agree, attaching a Guardrail to every
call (REQ-X-4.1). It reuses :class:`BedrockConfig` and the ``BedrockConverseClient``
Protocol from the enrichment layer rather than redefining them. CI never constructs this;
the deterministic stub (``eval.models.DeterministicSentimentJudge``) is used instead, and
the single live test that exercises this path is gated behind ``@pytest.mark.live``.
"""

from __future__ import annotations

from movieintel.config import BedrockConfig
from movieintel.domain.schemas import Sentiment
from movieintel.enrichment.client import BedrockConverseClient

# The judge is asked for a single-token verdict so the parse stays trivial and cheap.
_JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluation judge for sentiment labels. You are given a PRODUCED "
    "sentiment label and an EXPECTED sentiment label, each one of: positive, negative, "
    "neutral. Reply with exactly one word: YES if the produced label is a reasonable "
    "match for the expected label, otherwise NO. Output nothing else."
)


def _extract_text(response: dict[str, object]) -> str:
    """Pull the first text content block out of a Converse response (best-effort)."""
    output = response.get("output")
    if not isinstance(output, dict):
        return ""
    message = output.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


class BedrockSentimentJudge:
    """Bedrock-backed sentiment judge; attaches a Guardrail on every Converse call."""

    def __init__(self, *, client: BedrockConverseClient, config: BedrockConfig) -> None:
        self._client = client
        self._config = config

    def agree(self, produced: Sentiment, expected: Sentiment) -> bool:
        response = self._client.converse(
            modelId=self._config.model_id,
            system=[{"text": _JUDGE_SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": f"PRODUCED: {produced.value}\nEXPECTED: {expected.value}"}
                    ],
                }
            ],
            guardrailConfig=self._config.guardrail_config(),
        )
        return _extract_text(response).strip().upper().startswith("YES")
