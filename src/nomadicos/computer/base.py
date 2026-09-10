"""ComputerControl contract (BP §172, §177, §185, §209).

Actions are structured (kind + target), never free-form model text (BP §86).
Verification (BP §145) compares expected UI change against a post-action
observation; a click that "returned without error" is not success (BP §145).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nomadicos.core.errors import VerificationFailed


class ActionKind(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    KEY_PRESS = "key_press"
    SCROLL = "scroll"
    ACTIVATE_WINDOW = "activate_window"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class UIElement:
    """Grounded target: from accessibility tree or vision parse (BP §175)."""

    kind: str  # button / field / window / ...
    label: str | None = None
    position: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    window_title: str | None = None


@dataclass(frozen=True, slots=True)
class ComputerAction:
    kind: ActionKind
    target: UIElement | None = None
    coordinates: tuple[int, int] | None = None
    text: str | None = None
    key: str | None = None
    scroll_amount: int = 0
    expected_change: str | None = None  # BP §145: what should change on screen
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Evidence-bearing action result (BP §143, §146)."""

    action: ComputerAction
    success: bool
    verified: bool  # BP §145: screen observation confirmed the change
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def verify_against_expectation(
    before_hash: str | None,
    after_hash: str | None,
    expectation: str | None,
    parsed_elements: list[dict[str, Any]] | None = None,
) -> bool:
    """BP §145: expected UI change → capture → compare → confirm.

    A change is confirmed when the screen hash changed AND (when an
    expectation is given) the expected element text appears in the parsed
    post-action observation. If no expectation was declared, a screen change
    is the minimum evidence.
    """
    if before_hash is None or after_hash is None:
        return False
    if before_hash == after_hash:
        return False  # nothing changed → not verified
    if expectation is None:
        return True
    haystack = " ".join(
        str(e.get("label", "")) + " " + str(e.get("kind", ""))
        for e in (parsed_elements or [])
    ).lower()
    return expectation.lower() in haystack


class ComputerControl(ABC):
    """OS adapter interface (BP §172). Windows-first implementation (§173);
    structured API > accessibility > DOM > coordinates (§177)."""

    @abstractmethod
    async def screenshot_bytes(self) -> bytes:
        """Current screen as PNG bytes (feeds the vision engine)."""

    @abstractmethod
    async def screen_hash(self) -> str | None:
        """Content hash of the current screen for change detection."""

    @abstractmethod
    async def perform(self, action: ComputerAction) -> ActionResult:
        """Execute one action WITHOUT verification (low-level)."""

    async def perform_verified(
        self, action: ComputerAction, observation_provider: Any
    ) -> ActionResult:
        """Execute + verify per BP §145. `observation_provider` must expose
        ``observe(reason, question)`` returning a ScreenObservation."""
        before_hash = await self.screen_hash()
        result = await self.perform(action)
        if not result.success:
            return result
        observation = await observation_provider.observe(
            __import__(
                "nomadicos.vision.base", fromlist=["CaptureReason"]
            ).CaptureReason.STEP_VERIFY,
            action.expected_change,
        )
        verified = verify_against_expectation(
            before_hash,
            observation.content_hash,
            action.expected_change,
            observation.elements,
        )
        if not verified:
            raise VerificationFailed(
                f"action {action.kind.value} not verified on screen "
                f"(expected: {action.expected_change or 'any change'})",
                context={"before": before_hash, "after": observation.content_hash},
            )
        return ActionResult(
            action=action,
            success=True,
            verified=True,
            evidence={
                **result.evidence,
                "before_hash": before_hash,
                "after_hash": observation.content_hash,
                "expected_change": action.expected_change,
            },
        )


__all__ = [
    "ActionKind",
    "ActionResult",
    "ComputerAction",
    "ComputerControl",
    "UIElement",
    "verify_against_expectation",
]
