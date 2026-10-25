"""Tool bricks: filesystem + terminal + desktop + docker (SPEC §22-26)."""

from nomadicos.tools.base import Tool, ToolOutcome, ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.desktop import DesktopTool
from nomadicos.tools.docker import DockerTool, UnavailableDockerTool, docker_tool
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.terminal import ProcessSupervisor, TerminalTool

__all__ = [
    "DesktopTool",
    "DockerTool",
    "ExecutionContext",
    "FilesystemTool",
    "ProcessSupervisor",
    "TerminalTool",
    "Tool",
    "ToolOutcome",
    "ToolRegistry",
    "UnavailableDockerTool",
    "docker_tool",
]
