"""Root pytest configuration hook (post-release hardening).

Keeps pytest scratch files out of the working tree, which is often synced
by OneDrive (or similar) — file-sync locks on temp files surfaced as
``PermissionError`` during atomic replaces and test cleanup. The basetemp
resolves from the SYSTEM temp directory (TMPDIR/TEMP-aware, never a
personal path in the committed config) under a stable NomadicOS name that
does not collide with pytest's default ``pytest-of-<user>`` root (which can
be left with broken ACLs by elevated processes).

An explicit ``--basetemp`` command-line argument always wins.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def pytest_configure(config) -> None:
    if getattr(config.option, "basetemp", None) is None:
        base = Path(tempfile.gettempdir()) / "nomadicos-pytest"
        config.option.basetemp = str(base)
