"""``semantic_search`` tool: wraps the Task 7 KB Retrieve client (design §3.6, REQ-B-2.2).

Validates arguments against :class:`SemanticSearchArgs` (REQ-B-2.5), then delegates to
:func:`movieintel.kb.client.semantic_search` and maps each ``RetrievedMovie`` 1:1 to a
:class:`SemanticSearchHit`, carrying the retrieval metadata straight through (enums already
coerced by the client; an empty index yields ``count=0, hits=[]``). The function is named
``semantic_search_tool`` to avoid colliding with the KB client's ``semantic_search``.
"""

from __future__ import annotations

from typing import Any

from movieintel.agent.tools._ports import BedrockAgentRuntimeClient
from movieintel.agent.tools.args import SemanticSearchArgs, validate_args
from movieintel.agent.tools.results import (
    SemanticSearchHit,
    SemanticSearchResult,
    ToolValidationError,
)
from movieintel.kb.client import RetrievedMovie
from movieintel.kb.client import semantic_search as kb_semantic_search
from movieintel.kb.config import KBConfig


def semantic_search_tool(
    args: dict[str, Any],
    *,
    client: BedrockAgentRuntimeClient,
    config: KBConfig,
) -> SemanticSearchResult | ToolValidationError:
    """Semantic retrieval over enriched movie overviews via the Bedrock KB.

    Returns a :class:`SemanticSearchResult` on success or a :class:`ToolValidationError`
    when ``args`` fail validation (before the KB client is called).
    """
    validated = validate_args("semantic_search", SemanticSearchArgs, args)
    if isinstance(validated, ToolValidationError):
        return validated

    retrieved = kb_semantic_search(
        validated.query, client=client, config=config, top_k=validated.top_k
    )
    hits = [_to_hit(movie) for movie in retrieved]
    return SemanticSearchResult(query=validated.query, count=len(hits), hits=hits)


def _to_hit(movie: RetrievedMovie) -> SemanticSearchHit:
    return SemanticSearchHit(
        text=movie.text,
        score=movie.score,
        title=movie.title,
        genres=movie.genres,
        sentiment=movie.sentiment,
        mood=movie.mood,
        pes=movie.pes,
    )
