"""Durable append-only audit sink for the event logger (SPEC §35).

Without a sink every event, including grant / revoke / owner decisions, died with
the process. One JSON object per line, scrubbed of credential-shaped text.
It is DATA: it never authorizes, executes, or decides anything, and a write
failure is counted by the logger, never raised into the execution path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from nomadicos.kernel.events import Event
from nomadicos.memory.scrub import scrub

AUDIT_FILE = "audit-events.jsonl"


def jsonl_audit_sink(state_dir: str | Path) -> Callable[[Event], None]:
    """Sink appending each event to ``<state_dir>/audit-events.jsonl``.

    shortcut: the file grows without bound and is opened per event; rotate or
    ship it to PostgreSQL when volume matters.
    """
    path = Path(state_dir) / AUDIT_FILE

    def sink(event: Event) -> None:
        line = scrub(json.dumps(event.to_dict(), ensure_ascii=False, default=str))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return sink
