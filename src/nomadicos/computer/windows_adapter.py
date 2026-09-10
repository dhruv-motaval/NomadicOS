"""Windows adapter (BP §173) — lazy imports; hardware-marked tests only.

Prefers structured APIs over coordinates (BP §177): keyboard/mouse via
pyautogui-equivalents are the LAST resort. This adapter keeps everything
behind ComputerControl so Linux/macOS ports swap in later.
"""

import hashlib
from typing import Any

from nomadicos.computer.base import ActionResult, ComputerAction, ComputerControl
from nomadicos.core.errors import ValidationError
from nomadicos.core.logging import get_logger

logger = get_logger("computer.windows")

try:  # optional OS dependency
    import pyautogui

    PYAUTOGUI_AVAILABLE = True
except ImportError:  # pragma: no cover — CI has no pyautogui
    pyautogui = None
    PYAUTOGUI_AVAILABLE = False


class WindowsComputerControl(ComputerControl):
    """Windows-first adapter (BP §173). Coordinate actions are the fallback
    tier only; the Agent Runtime should prefer structured/accessibility paths."""

    def __init__(self, *, action_delay_seconds: float = 0.2) -> None:
        if not PYAUTOGUI_AVAILABLE:
            raise ValidationError(
                "pyautogui is not installed",
                context={"install": "pip install pyautogui pillow"},
            )
        pyautogui.FAILSAFE = True  # corner-escape remains available to the user
        self._delay = action_delay_seconds

    async def screenshot_bytes(self) -> bytes:
        import asyncio
        import io

        import PIL.ImageGrab

        def _grab() -> bytes:
            image = PIL.ImageGrab.grab()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        return await asyncio.get_running_loop().run_in_executor(None, _grab)

    async def screen_hash(self) -> str | None:
        try:
            data = await self.screenshot_bytes()
        except Exception:  # noqa: BLE001 — capture failure means unknown state
            return None
        return hashlib.sha256(data).hexdigest()[:16]

    async def perform(self, action: ComputerAction) -> ActionResult:
        import asyncio

        def _run() -> dict[str, Any]:
            evidence: dict[str, Any] = {"kind": action.kind.value}
            if action.kind.value == "click" and (action.coordinates or action.target):
                x, y = self._target_coordinates(action)
                pyautogui.click(x=x, y=y)
                evidence["clicked_at"] = [x, y]
            elif action.kind.value == "type_text" and action.text is not None:
                pyautogui.typewrite(action.text, interval=0.02)
                evidence["typed_chars"] = len(action.text)
            elif action.kind.value == "key_press" and action.key is not None:
                # Hotkey support: 'winleft+d' = hold winleft, tap d, release.
                # Single-key names pass through unchanged (BP §177 keyboard path).
                parts = [p.strip().lower() for p in action.key.split("+")]
                pressed: list[str] = []
                for part in parts[:-1]:
                    pyautogui.keyDown(part)
                    pressed.append(part)
                pyautogui.press(parts[-1])
                for part in reversed(pressed):
                    pyautogui.keyUp(part)
                evidence["key"] = action.key
                if len(parts) > 1:
                    evidence["hotkey"] = True
            elif action.kind.value == "scroll":
                pyautogui.scroll(action.scroll_amount)
                evidence["scroll"] = action.scroll_amount
            else:
                raise ValidationError(
                    f"unsupported action: {action.kind.value}",
                    context={"kind": action.kind.value},
                )
            return evidence

        try:
            evidence = await asyncio.get_running_loop().run_in_executor(None, _run)
        except ValidationError:
            raise
        except Exception as exc:
            return ActionResult(
                action=action, success=False, verified=False, error=str(exc)
            )
        return ActionResult(action=action, success=True, verified=False, evidence=evidence)

    @staticmethod
    def _target_coordinates(action: ComputerAction) -> tuple[int, int]:
        if action.coordinates is not None:
            return action.coordinates
        if action.target is not None and action.target.position is not None:
            return action.target.position
        raise ValidationError(
            "action requires coordinates or a grounded target position (BP §177: "
            "never guess coordinates)"
        )


__all__ = ["PYAUTOGUI_AVAILABLE", "WindowsComputerControl"]
