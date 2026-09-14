"""STEP 3.5 — real symlink / junction confinement of the filesystem tool.

Creates an actual link fixture on the real filesystem pointing OUTSIDE the
workspace, then proves read/write/delete are refused and the resolved target
is checked BEFORE any operation. Skips (explicitly, visibly) only if the
platform forbids every link mechanism available.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nomadicos.core.errors import ValidationError
from nomadicos.tools.filesystem import FilesystemTool


def _make_link(link: Path, target: Path) -> str | None:
    """Return created mechanism name or None if the platform refuses all."""
    if os.name == "nt":
        # junctions need no privileges on Windows
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and link.exists():
            return "junction"
    try:
        os.symlink(str(target.resolve()), str(link), target_is_directory=True)
        return "symlink"
    except OSError:
        try:
            os.symlink(str((target / "secret.txt").resolve()), str(link))
            return "file-symlink"
        except OSError:
            return None


def _cleanup(link: Path) -> None:
    """Remove the link itself (never its target)."""
    try:
        if os.name == "nt" and link.is_dir():
            link.rmdir()  # also removes junctions/symlinked dirs without touching target
        elif link.is_symlink() or link.is_file():
            link.unlink()
    except OSError:
        pass


@pytest.fixture()
def links(tmp_path) -> tuple[str, Path, Path, FilesystemTool]:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "allowed.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("DO-NOT-TOUCH", encoding="utf-8")
    link = ws / "link"
    made = _make_link(link, outside)
    if made is None:
        pytest.skip("UNVERIFIED — environment cannot create symlink/junction here")
    return made, link, outside, FilesystemTool(workspace_root=str(ws))


def test_junction_read_is_rejected_and_target_untouched(links) -> None:
    made, link, outside, tool = links
    with pytest.raises(ValidationError):
        _raise_via(tool, "read", "link/secret.txt")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "DO-NOT-TOUCH"


def _raise_via(tool: FilesystemTool, action: str, path: str) -> None:
    import asyncio

    asyncio.run(tool.validate_arguments({"action": action, "path": path}))


def test_write_through_link_rejected_before_touching_fs(links) -> None:
    made, link, outside, tool = links
    import asyncio

    with pytest.raises(ValidationError):
        asyncio.run(
            tool.validate_arguments({"action": "write", "path": "link/planted.txt", "content": "x"})
        )
    assert not (outside / "planted.txt").exists()


def test_delete_through_link_rejected(links) -> None:
    made, link, outside, tool = links
    import asyncio

    with pytest.raises(ValidationError):
        asyncio.run(tool.validate_arguments({"action": "delete", "path": "link/secret.txt"}))
    assert (outside / "secret.txt").exists()


def test_execute_layer_rejects_too(links) -> None:
    """Defense in depth: even bypassing validate_arguments, execute() resolves
    through the link and refuses."""
    made, link, outside, tool = links
    import asyncio

    from nomadicos.tools.base import ToolContext

    ctx = ToolContext(user_id="u")
    with pytest.raises(ValidationError):
        asyncio.run(tool.execute({"action": "read", "path": "link/secret.txt"}, ctx))
    with pytest.raises(ValidationError):
        asyncio.run(tool.execute({"action": "write", "path": "link/evil.txt", "content": "x"}, ctx))
    assert not (outside / "evil.txt").exists()
    _cleanup(link)


def test_confined_inside_operations_still_work(links) -> None:
    """Positive control: normal in-workspace ops still pass."""
    made, link, outside, tool = links
    import asyncio

    from nomadicos.tools.base import ToolContext

    ctx = ToolContext(user_id="u")
    res = asyncio.run(tool.execute({"action": "write", "path": "ok.txt", "content": "fine"}, ctx))
    assert res.success
