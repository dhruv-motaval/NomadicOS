"""Memory Engine (BP §16, §94, §112-113, §376-420; Phase 8).

Sessions are context boundaries, NOT memory boundaries (BP §377, §420).
Cross-session retrieval is allowed for the same owner by default, subject to
project scope, sensitivity, and policy (BP §381).
"""

from nomadicos.memory.engine import MemoryEngine

__all__ = ["MemoryEngine"]
