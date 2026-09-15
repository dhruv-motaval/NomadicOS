"""Agents brick: specialist workers on the existing graph (SPEC §53 Phase 9).

Only CodingWorker exists in Phase 9. Workers are PLAN + CONTEXT strategies
plugged into the Phase 7/8 graph - they never execute, authorize, or verify.
"""

from nomadicos.agents.base import Worker, WorkerReport
from nomadicos.agents.coding import CodingWorker, coding_context_builder
from nomadicos.agents.inspect import RepoInspector, RepoSurvey
from nomadicos.agents.planning import CodingPlanner

__all__ = [
    "CodingPlanner",
    "CodingWorker",
    "RepoInspector",
    "RepoSurvey",
    "Worker",
    "WorkerReport",
    "coding_context_builder",
]
