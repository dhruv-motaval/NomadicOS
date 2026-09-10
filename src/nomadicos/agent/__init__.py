"""Adaptive model selection (BP §148, §187, §320, §386-387; Phase 12).

ModelSelector learns from real experience (BP §64): global capability scores
are combined with per-task-family and per-project historical success
(BP §320: "Model B better for this user's codebase" can win). Selection is
deterministic and explainable (BP §225-226).
"""

from nomadicos.agent.selector import ModelSelector, SelectionCandidate
from nomadicos.agent.selector_policies import SelectionPolicy

__all__ = ["ModelSelector", "SelectionCandidate", "SelectionPolicy"]
