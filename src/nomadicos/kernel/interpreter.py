"""Runtime-interpreter resolution for owner/model-authored commands.

A bare ``python``/``python3`` token in a completion-command (e.g.
``python -m pytest -q``) means "the project's Python with the project's
dependencies" — which is the interpreter THIS runtime runs on (the project
venv). Resolving it from PATH instead can pick a base interpreter without
the project's test tooling (measured 2026-11-04: ``No module named pytest``
from uv's base python), making every honest test run fail on the
environment rather than the code. Deterministic replacement, no shell
semantics change: only the leading interpreter token is rewritten.
"""

from __future__ import annotations

import sys

BARE_PYTHON_TOKENS = frozenset({"python", "python3", "python.exe", "python3.exe"})


def is_bare_python(token: str) -> bool:
    return token.lower() in BARE_PYTHON_TOKENS


def runtime_python_argv_replacement(tokens: list[str]) -> list[str]:
    """argv-form: replace a leading bare interpreter token with the runtime's
    own interpreter (no quoting needed — argv, not shell text)."""
    if tokens and is_bare_python(tokens[0]):
        return [sys.executable, *tokens[1:]]
    return tokens


def runtime_python_shell_command(command: str) -> str:
    """shell-text form: quote the runtime interpreter (paths may contain
    spaces) and keep the remainder verbatim."""
    stripped = command.lstrip()
    head = stripped.split(None, 1)
    if head and is_bare_python(head[0]):
        rest = head[1] if len(head) > 1 else ""
        quoted = f'"{sys.executable}"'
        return f"{quoted} {rest}".rstrip()
    return command
