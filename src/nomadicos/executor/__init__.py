"""Executor brick (SPEC §21, §53 Phase 6): dispatch AuthorizedAction only."""

from nomadicos.executor.dispatch import DuplicateExecutionBlocked, Executor, InMemoryLedger

__all__ = ["DuplicateExecutionBlocked", "Executor", "InMemoryLedger"]
