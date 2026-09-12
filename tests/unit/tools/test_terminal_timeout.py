"""STEP 5 (live-trace finding): timed-out commands must not leak orphan shells."""
import asyncio
import os
import subprocess

from nomadicos.core.errors import ValidationError
from nomadicos.tools.base import ToolContext
from nomadicos.tools.terminal import TerminalTool


def _count(image: str) -> int:
    if os.name != "nt":
        out = subprocess.run(
            ["sh", "-c", f"pgrep -fc {image} 2>/dev/null || true"],
            capture_output=True,
            text=True,
        )
        try:
            return int(out.stdout.strip() or 0)
        except ValueError:
            return 0
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        capture_output=True,
        text=True,
    )
    return out.stdout.lower().count(image.lower())


def test_timeout_kills_child_no_orphan(tmp_path):
    tool = TerminalTool(workspace_root=str(tmp_path))
    ctx = ToolContext(user_id="u")
    image = "ping.exe" if os.name == "nt" else "ping"
    before = _count(image)
    try:
        asyncio.run(
            tool.execute(
                {
                    "command": image if os.name != "nt" else "ping",
                    "args": ["-n", "30", "127.0.0.1"],
                    "timeout_seconds": 0.6,
                },
                ctx,
            )
        )
    except ValidationError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("timeout must fail the step")
    # the killed child must be reaped: count cannot exceed the pre-existing set
    assert _count(image) <= before
