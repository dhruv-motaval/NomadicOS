"""Persistence error taxonomy (Phase 13A, SPEC §34).

Persistence failures are structured, never swallowed into implicit success:
corruption/incompatibility fails closed, unavailability is explicit. These
reuse the existing kernel error tree and categories - no new taxonomy.
"""

from __future__ import annotations

from nomadicos.kernel.errors import Failure, NomadicOSBaseError


class PersistenceError(NomadicOSBaseError):
    """Base for durable task-state store failures. Persistence is a DATA
    boundary: errors here are never converted into success or authority."""

    category = Failure.CONFIG_INVALID


class PersistenceUnavailable(PersistenceError):
    """The durable store could not be reached (DB down, DSN unusable)."""

    category = Failure.RESOURCE_UNAVAILABLE


class PersistenceCorrupt(PersistenceError):
    """Persisted record is missing, malformed, version-mismatched, or
    carries unknown fields. Restore fails closed - never guessed."""
