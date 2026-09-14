"""Fake ComputerControl — scripted actions + scripted verification for CI."""

import hashlib
from typing import Any

from nomadicos.computer.base import (
    ActionResult,
    ComputerAction,
    ComputerControl,
    verify_against_expectation,
)


class FakeComputerControl(ComputerControl):
    """Scripted screen states + action log. Verification uses the scripted
    post-action screen hash, so the full observe/verify loop runs in CI."""

    def __init__(self, screen: bytes = b"screen-initial") -> None:
        self._screen = screen
        self._post_action_screen: bytes | None = None
        self.performed: list[ComputerAction] = []
        self.fail_next_action = False

    # ------------------------------------------------------------- scripting

    def set_screen(self, payload: bytes) -> None:
        self._screen = payload

    def set_post_action_screen(self, payload: bytes) -> None:
        """What the screen becomes after the next action (verification input)."""
        self._post_action_screen = payload

    # -------------------------------------------------------------- contract

    async def screenshot_bytes(self) -> bytes:
        return self._screen

    async def screen_hash(self) -> str | None:
        return hashlib.sha256(self._screen).hexdigest()[:16]

    async def perform(self, action: ComputerAction) -> ActionResult:
        self.performed.append(action)
        if self.fail_next_action:
            self.fail_next_action = False
            return ActionResult(
                action=action, success=False, verified=False, error="scripted failure"
            )
        evidence: dict[str, Any] = {
            "kind": action.kind.value,
            "target": action.target.label if action.target else None,
        }
        post = self._post_action_screen
        if post is not None:
            before_hash = hashlib.sha256(self._screen).hexdigest()[:16]
            after_hash = hashlib.sha256(post).hexdigest()[:16]
            evidence.update({"before_hash": before_hash, "after_hash": after_hash})
            verified = verify_against_expectation(before_hash, after_hash, action.expected_change)
            evidence["verified"] = verified
            self._screen = post
            self._post_action_screen = None
            return ActionResult(action=action, success=True, verified=verified, evidence=evidence)
        return ActionResult(action=action, success=True, verified=False, evidence=evidence)


__all__ = ["FakeComputerControl"]
