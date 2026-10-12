"""Tool bricks: filesystem + terminal + desktop (SPEC §22-26)."""

from nomadicos.tools.base import Tool, ToolOutcome, ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.desktop import DesktopTool
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.terminal import ProcessSupervisor, TerminalTool

__all__ = [
    "DesktopTool",
    "ExecutionContext",
    "FilesystemTool",
    "ProcessSupervisor",
    "Tool",
    "ToolOutcome",
    "ToolRegistry",
    "TerminalTool",
]
