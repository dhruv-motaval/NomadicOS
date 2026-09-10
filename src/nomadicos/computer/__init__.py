"""Computer Control (BP §7, §50-51, §145, §172-177, §240; Phase 6).

Structured action first, accessibility second, vision third (BP §177). Every
action is verified by screen observation — never blind (BP §145, §240).
All actions are mediated by the Security Gate at the Agent Runtime layer.
"""

from nomadicos.computer.base import (
    ActionResult,
    ComputerAction,
    ComputerControl,
    UIElement,
    verify_against_expectation,
)
from nomadicos.computer.fake import FakeComputerControl

__all__ = [
    "ActionResult",
    "ComputerAction",
    "ComputerControl",
    "FakeComputerControl",
    "UIElement",
    "verify_against_expectation",
]
