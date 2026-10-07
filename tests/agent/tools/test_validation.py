"""Invalid-args rejection per tool: structured error, NO execution (REQ-B-2.5).

Every case asserts the tool returns a :class:`ToolValidationError` (with populated
``detail``) and that the injected repository / KB stub recorded ZERO calls — proving
validation runs BEFORE any execution.
"""

from __future__ import annotations

from movieintel.agent.tools.compare_movies import compare_movies
from movieintel.agent.tools.query_movies import query_movies
from movieintel.agent.tools.results import ToolValidationError
from movieintel.agent.tools.semantic_search import semantic_search_tool
from movieintel.kb.config import KBConfig

from .conftest import FakeAgentRuntime, StubRepository


def _kb_config() -> KBConfig:
    return KBConfig.from_env(env={"MOVIEINTEL_KB_ID": "KB0ABC1234"})


# --- query_movies ----------------------------------------------------------------------


def test_query_movies_rejects_out_of_enum_sentiment() -> None:
    repo = StubRepository()
    result = query_movies({"sentiment": "ecstatic"}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert result.tool == "query_movies"
    assert result.detail
    assert repo.calls == []


def test_query_movies_rejects_limit_below_range() -> None:
    repo = StubRepository()
    result = query_movies({"limit": 0}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []


def test_query_movies_rejects_limit_above_range() -> None:
    repo = StubRepository()
    result = query_movies({"limit": 51}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []


def test_query_movies_rejects_out_of_enum_sort_by() -> None:
    repo = StubRepository()
    result = query_movies({"sort_by": "mood"}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []


def test_query_movies_rejects_unknown_property() -> None:
    repo = StubRepository()
    result = query_movies({"nonsense": 1}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []


# --- semantic_search -------------------------------------------------------------------


def test_semantic_search_rejects_missing_query() -> None:
    kb = FakeAgentRuntime()
    result = semantic_search_tool({}, client=kb, config=_kb_config())
    assert isinstance(result, ToolValidationError)
    assert result.tool == "semantic_search"
    assert kb.calls == []


def test_semantic_search_rejects_empty_query() -> None:
    kb = FakeAgentRuntime()
    result = semantic_search_tool({"query": ""}, client=kb, config=_kb_config())
    assert isinstance(result, ToolValidationError)
    assert kb.calls == []


def test_semantic_search_rejects_top_k_below_range() -> None:
    kb = FakeAgentRuntime()
    result = semantic_search_tool({"query": "x", "top_k": 0}, client=kb, config=_kb_config())
    assert isinstance(result, ToolValidationError)
    assert kb.calls == []


def test_semantic_search_rejects_top_k_above_range() -> None:
    kb = FakeAgentRuntime()
    result = semantic_search_tool({"query": "x", "top_k": 21}, client=kb, config=_kb_config())
    assert isinstance(result, ToolValidationError)
    assert kb.calls == []


def test_semantic_search_rejects_unknown_property() -> None:
    kb = FakeAgentRuntime()
    result = semantic_search_tool({"query": "x", "bogus": 1}, client=kb, config=_kb_config())
    assert isinstance(result, ToolValidationError)
    assert kb.calls == []


# --- compare_movies --------------------------------------------------------------------


def test_compare_movies_rejects_single_movie_id() -> None:
    repo = StubRepository()
    result = compare_movies({"movie_ids": ["1"]}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert result.tool == "compare_movies"
    assert repo.calls == []


def test_compare_movies_rejects_unknown_dimension() -> None:
    repo = StubRepository()
    result = compare_movies(
        {"movie_ids": ["1", "2"], "dimensions": ["popularity"]}, repository=repo
    )
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []


def test_compare_movies_rejects_unknown_property() -> None:
    repo = StubRepository()
    result = compare_movies({"movie_ids": ["1", "2"], "extra": True}, repository=repo)
    assert isinstance(result, ToolValidationError)
    assert repo.calls == []
