"""Router brick: deterministic task analysis, smallest-capable selection,
bounded escalation (SPEC §13-14, §52B)."""

from nomadicos.router.analyzer import analyze_goal
from nomadicos.router.escalation import EscalationPolicy
from nomadicos.router.selection import CandidateScore, ModelSelector, SelectionResult

__all__ = [
    "CandidateScore",
    "EscalationPolicy",
    "ModelSelector",
    "SelectionResult",
    "analyze_goal",
]
