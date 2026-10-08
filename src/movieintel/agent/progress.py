"""Agent-loop progress primitives (design Decision 4).

Pure-Python types describing the real per-phase position of ``run_agent``'s orchestration
loop. ``run_agent`` emits a :class:`ProgressEvent` at each verified loop point via an
optional, backward-compatible ``on_progress`` callback; when no callback is supplied the
loop never constructs or dispatches an event. These types carry no AWS dependency so the
loop can import them without a cycle, and the serving layer re-uses :class:`ProgressPhase`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["PHASE_LABELS", "ProgressEvent", "ProgressPhase"]


class ProgressPhase(StrEnum):
    """The real agent-loop phase a :class:`ProgressEvent` reports."""

    understanding = "understanding"  # loop start, before first converse
    searching = "searching"  # query_movies / semantic_search dispatched
    comparing = "comparing"  # compare_movies dispatched
    composing = "composing"  # final_answer received (validating/returning)
    refused = "refused"  # guardrail intervention
    bounded = "bounded"  # max_turns reached


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A single immutable progress emission from the agent loop."""

    phase: ProgressPhase
    turn: int  # 1-based turn index at emission (0 at loop start)
    label: str  # human-readable, from PHASE_LABELS[phase]


PHASE_LABELS: dict[ProgressPhase, str] = {
    ProgressPhase.understanding: "understanding your question",
    ProgressPhase.searching: "searching the catalog",
    ProgressPhase.comparing: "comparing titles",
    ProgressPhase.composing: "composing the answer",
    ProgressPhase.refused: "request declined",
    ProgressPhase.bounded: "stopped at the turn limit",
}
