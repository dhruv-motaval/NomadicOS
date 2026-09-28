"""Brain error taxonomy (NomadicBrain spec §6, §28; NomadicOS SPEC §30).

Brain failures are structural and deterministic: malformed task graphs,
incoherent state, and rejected transitions fail closed and are never
coerced into success. The taxonomy extends the kernel error tree instead
of replacing it, so brain failures remain attributable through the
existing ``Failure`` categories.
"""

from __future__ import annotations

from nomadicos.kernel.errors import Failure, NomadicOSBaseError


class BrainError(NomadicOSBaseError):
    """Root of the brain error tree (malformed structure/state)."""

    category = Failure.CONFIG_INVALID


class ModelOutputRejected(BrainError):
    """Model-authored cognitive output failed typed validation (fail closed).

    Category MODEL_ERROR: the output never becomes a task graph, a
    candidate, or a monitoring decision — malformed proposals are
    rejected, never coerced (NomadicOS SPEC §56.14).
    """

    category = Failure.MODEL_ERROR


class InvalidTaskGraph(BrainError):
    """A task graph is structurally invalid (duplicate id, unknown or cyclic
    dependency, dangling subgoal reference, incoherent statuses)."""


class InvalidBrainState(BrainError):
    """A brain state is incoherent (schema version, cross references)."""


class InvalidTransition(BrainError):
    """A deterministic state transition was rejected (fail closed)."""
