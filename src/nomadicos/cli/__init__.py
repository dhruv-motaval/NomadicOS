"""CLI brick (SPEC §40): thin wrapper over the application layer.

Business logic lives in bricks, not the CLI. Currently exposes health,
model listing/sync; run/chat/task-status land with orchestration phases.
"""

from nomadicos.cli.app import app, main_entry

__all__ = ["app", "main_entry"]
