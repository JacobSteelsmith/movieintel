"""Agent tools package: the three tools + their Converse ``toolConfig`` specs (design §3.6).

Public surface for the agent loop (Task 9): the three tool entrypoints, their Pydantic args
and result models, the structured validation error, and the Converse ``toolConfig`` specs
(``ALL_TOOL_SPECS`` / ``by_name``). Each tool validates its arguments and returns either a
Pydantic-validated result or a :class:`ToolValidationError`.
"""

from __future__ import annotations

from movieintel.agent.tools._ports import BedrockAgentRuntimeClient, MovieRepository
from movieintel.agent.tools.args import (
    CompareMoviesArgs,
    QueryMoviesArgs,
    SemanticSearchArgs,
    validate_args,
)
from movieintel.agent.tools.compare_movies import compare_movies
from movieintel.agent.tools.query_movies import query_movies
from movieintel.agent.tools.results import (
    CompareMoviesMovie,
    CompareMoviesResult,
    DimensionComparison,
    QueryMoviesMovie,
    QueryMoviesResult,
    SemanticSearchHit,
    SemanticSearchResult,
    ToolValidationError,
)
from movieintel.agent.tools.semantic_search import semantic_search_tool
from movieintel.agent.tools.specs import (
    ALL_TOOL_SPECS,
    COMPARE_MOVIES_SPEC,
    QUERY_MOVIES_SPEC,
    SEMANTIC_SEARCH_SPEC,
    by_name,
)

__all__ = [
    "ALL_TOOL_SPECS",
    "COMPARE_MOVIES_SPEC",
    "QUERY_MOVIES_SPEC",
    "SEMANTIC_SEARCH_SPEC",
    "BedrockAgentRuntimeClient",
    "CompareMoviesArgs",
    "CompareMoviesMovie",
    "CompareMoviesResult",
    "DimensionComparison",
    "MovieRepository",
    "QueryMoviesArgs",
    "QueryMoviesMovie",
    "QueryMoviesResult",
    "SemanticSearchArgs",
    "SemanticSearchHit",
    "SemanticSearchResult",
    "ToolValidationError",
    "by_name",
    "compare_movies",
    "query_movies",
    "semantic_search_tool",
    "validate_args",
]
