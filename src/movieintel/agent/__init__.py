"""Agentic serving subsystem (design §3.6-§3.8).

Task 8 populates :mod:`movieintel.agent.tools` — the three pure, independently testable
tools plus their Converse ``toolConfig`` specs. Task 9 adds the explicit Converse
orchestration loop (§3.7): :func:`run_agent` drives the movie-assistant conversation with
those tools plus a ``final_answer`` tool and returns a validated :class:`AgentResult`. The
public surface is re-exported here for ergonomic imports, mirroring the other layers. The
serving layer (§3.8) is a later task.
"""

from __future__ import annotations

from movieintel.agent.loop import (
    MAX_TURNS,
    ToolDispatcher,
    is_guardrail_intervention,
    run_agent,
)
from movieintel.agent.response import (
    AgentResult,
    BoundedResponse,
    ComparativeAnalysis,
    PreferenceSummary,
    RecommendationList,
    RecommendedMovie,
    RefusalResponse,
)

__all__ = [
    "MAX_TURNS",
    "AgentResult",
    "BoundedResponse",
    "ComparativeAnalysis",
    "PreferenceSummary",
    "RecommendationList",
    "RecommendedMovie",
    "RefusalResponse",
    "ToolDispatcher",
    "is_guardrail_intervention",
    "run_agent",
]
