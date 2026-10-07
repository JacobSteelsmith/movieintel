"""semantic_search tests: Retrieve request shape, parsing, metadata passthrough (REQ-B-1).

Mirrors ``tests/persistence/test_vectors.py``: a botocore ``Stubber`` drives a real
``bedrock-agent-runtime`` client so no live AWS is touched, and a ``FakeAgentRuntime``
captures the request kwargs for request-shape assertions. The S3 Vectors index is
currently EMPTY, so empty-result handling is a first-class case.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.stub import Stubber

from movieintel.domain.schemas import Mood, Sentiment
from movieintel.kb.client import RetrievedMovie, semantic_search
from movieintel.kb.config import KBConfig


class FakeAgentRuntime:
    """Capture the ``retrieve`` kwargs and return a canned response (request-shape probe)."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def _result(
    *,
    text: str = "A dark thriller.",
    score: float = 0.87,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Retrieve ``retrievalResults`` entry."""
    return {
        "content": {"text": text},
        "score": score,
        "location": {"type": "S3"},
        "metadata": metadata if metadata is not None else {},
    }


def _config() -> KBConfig:
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "KB0ABC1234"})


def test_request_shape_uses_configured_kb_id_and_query() -> None:
    """The Retrieve request carries the configured KB id, the query text, and numberOfResults."""
    fake = FakeAgentRuntime({"retrievalResults": []})
    semantic_search("a tense heist movie", client=fake, config=_config())

    (call,) = fake.calls
    assert call["knowledgeBaseId"] == "KB0ABC1234"
    assert call["retrievalQuery"] == {"text": "a tense heist movie"}
    vector_config = call["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vector_config["numberOfResults"] == 5


def test_top_k_overrides_config_number_of_results() -> None:
    """An explicit top_k wins over the config default for numberOfResults."""
    fake = FakeAgentRuntime({"retrievalResults": []})
    semantic_search("x", client=fake, config=_config(), top_k=3)

    (call,) = fake.calls
    vector_config = call["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vector_config["numberOfResults"] == 3


def test_parses_results_and_passes_metadata_through() -> None:
    """Each retrieval result maps to a RetrievedMovie carrying the vector metadata."""
    fake = FakeAgentRuntime(
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
    results = semantic_search("uplifting drama", client=fake, config=_config())

    assert results == [
        RetrievedMovie(
            text="An uplifting drama.",
            score=0.91,
            title="The Example",
            genres='["Drama"]',
            sentiment=Sentiment.POSITIVE,
            mood=Mood.UPLIFTING,
            pes=77.5,
        )
    ]


def test_empty_results_returns_empty_list() -> None:
    """The index is currently EMPTY: Retrieve returns no results and we return []."""
    fake = FakeAgentRuntime({"retrievalResults": []})
    assert semantic_search("anything", client=fake, config=_config()) == []


def test_missing_retrieval_results_key_returns_empty_list() -> None:
    """A response with no retrievalResults key degrades to [] rather than raising."""
    fake = FakeAgentRuntime({})
    assert semantic_search("anything", client=fake, config=_config()) == []


def test_partial_metadata_degrades_without_fabrication() -> None:
    """Missing metadata fields map to None (never fabricated), text/score still parse."""
    fake = FakeAgentRuntime(
        {"retrievalResults": [_result(text="Sparse.", score=0.5, metadata={"title": "Only Title"})]}
    )
    (movie,) = semantic_search("q", client=fake, config=_config())
    assert movie.title == "Only Title"
    assert movie.genres is None
    assert movie.sentiment is None
    assert movie.mood is None
    assert movie.pes is None
    assert movie.text == "Sparse."
    assert movie.score == 0.5


def test_unknown_metadata_enum_values_degrade_to_none() -> None:
    """An out-of-set sentiment/mood value does not crash parsing; it degrades to None."""
    fake = FakeAgentRuntime(
        {
            "retrievalResults": [
                _result(metadata={"sentiment": "ecstatic", "mood": "zany", "pes": "not-a-number"})
            ]
        }
    )
    (movie,) = semantic_search("q", client=fake, config=_config())
    assert movie.sentiment is None
    assert movie.mood is None
    assert movie.pes is None


def test_raises_when_kb_id_unconfigured() -> None:
    """With no KB id configured, semantic_search raises before calling retrieve."""
    import pytest

    from movieintel.kb.config import KnowledgeBaseNotConfiguredError

    fake = FakeAgentRuntime({"retrievalResults": []})
    with pytest.raises(KnowledgeBaseNotConfiguredError):
        semantic_search("q", client=fake, config=KBConfig.from_env(env={}))
    assert fake.calls == []


def test_works_with_a_real_stubbed_bedrock_agent_runtime_client() -> None:
    """Integration-shape: a botocore Stubber on the real client satisfies the Protocol."""
    config = _config()
    client = boto3.client(
        "bedrock-agent-runtime",
        region_name=config.region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    response = {
        "retrievalResults": [
            {
                "content": {"text": "A tense heist."},
                "score": 0.8,
                "location": {"type": "S3"},
                "metadata": {"title": "Heist", "mood": "tense"},
            }
        ]
    }
    with Stubber(client) as stubber:
        stubber.add_response(
            "retrieve",
            response,
            expected_params={
                "knowledgeBaseId": "KB0ABC1234",
                "retrievalQuery": {"text": "heist"},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 5}},
            },
        )
        results = semantic_search("heist", client=client, config=config)
        stubber.assert_no_pending_responses()

    assert len(results) == 1
    assert results[0].title == "Heist"
    assert results[0].mood == Mood.TENSE
