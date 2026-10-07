"""semantic_search tool unit tests (design §3.6; REQ-B-2.2, X-1.1).

The tool wraps ``kb.client.semantic_search`` and maps each ``RetrievedMovie`` 1:1 to a
``SemanticSearchHit``, carrying metadata straight through. Driven by the ``FakeAgentRuntime``
KB stub; empty-index handling is a first-class case.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.tools.results import SemanticSearchResult
from movieintel.agent.tools.semantic_search import semantic_search_tool
from movieintel.domain.schemas import Mood, Sentiment
from movieintel.kb.config import KBConfig

from .conftest import FakeAgentRuntime


def _config() -> KBConfig:
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "KB0ABC1234"})


def _result(
    *, text: str = "A film.", score: float = 0.8, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"content": {"text": text}, "score": score, "metadata": metadata or {}}


def test_maps_hits_and_passes_metadata_through() -> None:
    kb = FakeAgentRuntime(
        {
            "retrievalResults": [
                _result(
                    text="An uplifting drama.",
                    score=0.91,
                    metadata={
                        "title": "The Example",
                        "genres": '["Drama"]',
                        "sentiment": "positive",
                        "mood": "uplifting",
                        "pes": 77.5,
                    },
                )
            ]
        }
    )
    result = semantic_search_tool({"query": "uplifting drama"}, client=kb, config=_config())
    assert isinstance(result, SemanticSearchResult)
    assert result.query == "uplifting drama"
    assert result.count == 1
    hit = result.hits[0]
    assert hit.text == "An uplifting drama."
    assert hit.score == 0.91
    assert hit.title == "The Example"
    assert hit.genres == '["Drama"]'
    assert hit.sentiment is Sentiment.POSITIVE
    assert hit.mood is Mood.UPLIFTING
    assert hit.pes == 77.5


def test_top_k_forwarded_to_kb_client() -> None:
    kb = FakeAgentRuntime({"retrievalResults": []})
    semantic_search_tool({"query": "x", "top_k": 3}, client=kb, config=_config())
    (call,) = kb.calls
    vector_config = call["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vector_config["numberOfResults"] == 3


def test_empty_index_returns_zero_hits() -> None:
    kb = FakeAgentRuntime({"retrievalResults": []})
    result = semantic_search_tool({"query": "anything"}, client=kb, config=_config())
    assert isinstance(result, SemanticSearchResult)
    assert result.count == 0
    assert result.hits == []


def test_partial_metadata_degrades_to_none() -> None:
    kb = FakeAgentRuntime(
        {"retrievalResults": [_result(text="Sparse.", metadata={"title": "Only Title"})]}
    )
    result = semantic_search_tool({"query": "q"}, client=kb, config=_config())
    assert isinstance(result, SemanticSearchResult)
    hit = result.hits[0]
    assert hit.title == "Only Title"
    assert hit.genres is None
    assert hit.sentiment is None
    assert hit.mood is None
    assert hit.pes is None
