"""Evaluation Engine (BP §28, §100, §144-146, §366; Phase 10).

Deterministic verification: a task run is verified by evidence checks, never
by model claims (BP §366). Scoring aggregates verification + retry + budget
discipline into comparable per-run records.
"""

from nomadicos.evaluation.engine import EvaluationEngine, RunRecord
from nomadicos.evaluation.model_eval import ModelPerformanceTracker

__all__ = ["EvaluationEngine", "ModelPerformanceTracker", "RunRecord"]
