"""Explicit Converse-API agent orchestration loop (Task 9, design §3.7, REQ-B-3).

``run_agent`` drives a movie-assistant conversation through the Bedrock Converse API with
the three Task 8 tools plus a dedicated ``final_answer`` tool:

1. Build the initial Converse request — a system prompt framing the assistant role and the
   available tools, the user message, ``toolConfig = ALL_TOOL_SPECS + [FINAL_ANSWER_SPEC]``,
   and ``guardrailConfig`` from :class:`BedrockConfig` attached on EVERY call (REQ-X-4.1).
2. Loop up to ``max_turns`` (default :data:`MAX_TURNS`):

   - If the response signals a Guardrail intervention (:func:`is_guardrail_intervention`,
     which checks more than ``stopReason``), return a safe structured refusal WITHOUT
     dispatching any tool or reading any content (REQ-B-3.4/.5, REQ-X-4.3).
   - If ``stopReason == "tool_use"``, dispatch each requested tool and append the matching
     ``toolResult`` blocks (in request order) as one user turn, then continue. A
     ``final_answer`` call ends the loop with a validated :class:`AgentResult`.
   - Otherwise (the model stopped without calling ``final_answer``) return a bounded
     response rather than fabricating a result.

3. Reaching ``max_turns`` returns a :class:`BoundedResponse` — never an exception, never an
   infinite loop (REQ-B-3.2).

Every per-request problem (unknown tool, invalid args, max turns, guardrail) is a structured
outcome, never a raise — exactly like ``enrich_movie``'s result union. Bedrock, the
repository, and the KB client are all injected via Protocols so CI mocks them; the model id
and Guardrail come only from :class:`BedrockConfig` (no inline literals).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from movieintel.agent.response import (
    FINAL_ANSWER_SPEC,
    AgentResult,
    FinalAnswerError,
    UnknownToolError,
    bounded_response,
    parse_final_answer,
    safe_refusal,
)
from movieintel.agent.tools import (
    ALL_TOOL_SPECS,
    MovieRepository,
    compare_movies,
    query_movies,
    semantic_search_tool,
)
from movieintel.agent.tools._ports import BedrockAgentRuntimeClient
from movieintel.config import BedrockConfig

# Reuse the enrichment Converse Protocol rather than redefining it (design §3.3).
from movieintel.enrichment.client import BedrockConverseClient
from movieintel.kb.config import KBConfig

__all__ = ["MAX_TURNS", "ToolDispatcher", "is_guardrail_intervention", "run_agent"]

# Default safety bound on orchestration turns (REQ-B-3.2); overridable per call via
# ``run_agent(..., max_turns=...)`` and as this module constant (mirrors
# ``enrichment.client.MAX_REPAIR_ATTEMPTS``).
MAX_TURNS = 6

# Converse ``stopReason`` emitted when a Guardrail intervenes (same literal as Task 3).
_GUARDRAIL_STOP_REASON = "guardrail_intervened"

# The ``final_answer`` tool name the model calls to terminate the loop with a structured
# result (its spec is :data:`FINAL_ANSWER_SPEC`).
_FINAL_ANSWER_TOOL = "final_answer"


def is_guardrail_intervention(response: dict[str, Any]) -> bool:
    """Return True when a Converse response indicates a Guardrail blocked the generation.

    Detection is centralized here and intentionally checks MORE than the single
    ``stopReason`` value (REQ-X-4.1), reading ONLY envelope metadata — never message
    content — so an intervened response is caught before any content is parsed (REQ-X-4.3).
    Any of these signals marks an intervention:

    - ``stopReason == "guardrail_intervened"`` (the primary signal).
    - A guardrail trace block is present and non-empty: ``response["trace"]["guardrail"]``
      is a non-empty dict (an intervened response carries this even when the inner
      assessment map is empty, so the PRESENCE of the block is the signal).
    - An explicit blocked/action indicator: ``action == "GUARDRAIL_INTERVENED"`` or a
      ``guardrailAction``/``amazon-bedrock-guardrailAction`` of ``"INTERVENED"`` (defensive
      against envelope variants).
    """
    if response.get("stopReason") == _GUARDRAIL_STOP_REASON:
        return True

    trace = response.get("trace")
    if isinstance(trace, dict):
        guardrail = trace.get("guardrail")
        if isinstance(guardrail, dict) and guardrail:
            return True

    if response.get("action") == "GUARDRAIL_INTERVENED":
        return True
    return (
        response.get("guardrailAction") == "INTERVENED"
        or response.get("amazon-bedrock-guardrailAction") == "INTERVENED"
    )


class ToolDispatcher:
    """Maps a tool name to its Task 8 callable + injected dependencies (design §3.7).

    The three Task 8 tools have different dependency signatures (``query_movies`` /
    ``compare_movies`` take ``repository=``; ``semantic_search`` takes ``client=`` +
    ``config=``), so each registry entry is a closure adapting the uniform
    ``dispatch(name, input_dict)`` call. An unknown tool name returns a structured
    :class:`UnknownToolError` rather than raising, so a hallucinated tool never crashes the
    loop (REQ-B-3 robustness). Tool logic is NOT reimplemented here — the Task 8 tools are
    called as-is, so their own argument validation (REQ-B-2.5) still applies.
    """

    def __init__(
        self,
        repository: MovieRepository,
        kb_client: BedrockAgentRuntimeClient,
        kb_config: KBConfig,
    ) -> None:
        self._registry: dict[str, Callable[[dict[str, Any]], BaseModel]] = {
            "query_movies": lambda raw: query_movies(raw, repository=repository),
            "compare_movies": lambda raw: compare_movies(raw, repository=repository),
            "semantic_search": lambda raw: semantic_search_tool(
                raw, client=kb_client, config=kb_config
            ),
        }

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> BaseModel:
        """Run the named tool with ``tool_input``; unknown names return ``UnknownToolError``.

        Returns the tool's Pydantic result (or its ``ToolValidationError`` for invalid
        args) for a known tool, or an :class:`UnknownToolError` for an unknown one. Never
        raises for a per-request problem.
        """
        handler = self._registry.get(name)
        if handler is None:
            return UnknownToolError(tool=name)
        return handler(tool_input)


def build_agent_system_prompt() -> str:
    """Frame the movie-assistant role and name the tools available to the model."""
    return (
        "You are a movie intelligence assistant. Answer questions about enriched movies "
        "using ONLY the provided tools:\n"
        "- query_movies: filter movies by sentiment and numeric ranges, ranked by "
        "Production Effectiveness Score (PES).\n"
        "- semantic_search: semantic retrieval over movie overviews.\n"
        "- compare_movies: deterministic side-by-side comparison of specific movies.\n"
        "Gather what you need with those tools, then call the final_answer tool exactly "
        "once with the appropriate structured 'kind' (recommendations, preferences, or "
        "comparison). Do not fabricate movie facts; rely on the tool results.\n"
        "Make at most two tool-search attempts. If the tools return few or no matches, do "
        "NOT keep searching or repeat near-identical queries — call final_answer and "
        "report what you found, using an empty or short movies list with a query_summary "
        "that names the closest available results (e.g. 'no exact matches; closest "
        "available: ...'). Prefer answering from the results you already have over hunting "
        "for a better set."
    )


def _tool_config() -> dict[str, Any]:
    """Compose the Converse ``toolConfig`` as the three Task 8 tools + ``final_answer``."""
    return {"tools": [*ALL_TOOL_SPECS, FINAL_ANSWER_SPEC]}


def _request(
    *,
    config: BedrockConfig,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_config: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a Converse request with the Guardrail attached on every call (REQ-X-4.1)."""
    return {
        "modelId": config.model_id,
        "system": [{"text": system_prompt}],
        "messages": messages,
        "toolConfig": tool_config,
        "guardrailConfig": config.guardrail_config(),
    }


def _assistant_turn(response: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the assistant message so the model sees its own tool_use on continue."""
    message = response.get("output", {}).get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return {"role": "assistant", "content": message.get("content", [])}
    return {"role": "assistant", "content": [{"text": ""}]}


def _tool_use_blocks(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the ``toolUse`` payloads from a Converse response, in order."""
    content = response.get("output", {}).get("message", {}).get("content", [])
    return [block["toolUse"] for block in content if isinstance(block, dict) and "toolUse" in block]


def _tool_result_block(tool_use_id: str, result: BaseModel, *, is_error: bool) -> dict[str, Any]:
    """Build a Converse ``toolResult`` content block carrying a structured JSON result.

    On an error (``ToolValidationError`` or unknown tool), ``status="error"`` is set and
    the structured error is placed under the ``json`` content so the model can SEE why and
    retry (REQ-B-2.5 surfaced, not crashed).
    """
    block: dict[str, Any] = {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": result.model_dump(mode="json")}],
        }
    }
    if is_error:
        block["toolResult"]["status"] = "error"
    return block


def _is_error_result(result: BaseModel) -> bool:
    """True when a dispatched result is a structured error (invalid args / unknown tool)."""
    # ToolValidationError carries error="invalid_arguments"; UnknownToolError carries
    # error="unknown_tool". Both expose an ``error`` field; real results do not.
    return hasattr(result, "error")


def run_agent(
    user_request: str,
    *,
    client: BedrockConverseClient,
    repository: MovieRepository,
    kb_client: BedrockAgentRuntimeClient,
    kb_config: KBConfig,
    config: BedrockConfig,
    max_turns: int = MAX_TURNS,
) -> AgentResult:
    """Run the explicit Converse orchestration loop for ``user_request`` (design §3.7).

    Returns a validated :class:`AgentResult`: one of the three terminal result shapes when
    the model calls ``final_answer``, a :class:`RefusalResponse` on a Guardrail
    intervention, or a :class:`BoundedResponse` on the ``max_turns`` bound. A per-request
    problem (unknown tool, invalid args, bound, guardrail) is always a structured outcome,
    never a raise.
    """
    system_prompt = build_agent_system_prompt()
    tool_config = _tool_config()
    dispatcher = ToolDispatcher(repository, kb_client, kb_config)
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": user_request}]}]

    for turn in range(max_turns):
        response = client.converse(
            **_request(
                config=config,
                system_prompt=system_prompt,
                messages=messages,
                tool_config=tool_config,
            )
        )

        # Guardrail interventions are checked FIRST — never dispatch or parse blocked content.
        if is_guardrail_intervention(response):
            return safe_refusal()

        if response.get("stopReason") != "tool_use":
            # Model stopped without calling final_answer; do not fabricate a result.
            return bounded_response(turns_used=turn + 1)

        messages.append(_assistant_turn(response))
        tool_results: list[dict[str, Any]] = []
        for block in _tool_use_blocks(response):
            name = block.get("name", "")
            tool_use_id = block.get("toolUseId", "")
            tool_input = block.get("input") or {}

            if name == _FINAL_ANSWER_TOOL:
                parsed = parse_final_answer(tool_input)
                if not isinstance(parsed, FinalAnswerError):
                    return parsed
                # Invalid final answer: surface the error so the model can correct, then
                # keep looping (never return an unvalidated object — REQ-X-1).
                tool_results.append(_tool_result_block(tool_use_id, parsed, is_error=True))
                continue

            result = dispatcher.dispatch(name, tool_input)
            tool_results.append(
                _tool_result_block(tool_use_id, result, is_error=_is_error_result(result))
            )

        messages.append({"role": "user", "content": tool_results})

    return bounded_response(turns_used=max_turns)
