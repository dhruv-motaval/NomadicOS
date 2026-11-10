"""Desktop tool (SPEC §26, §53 Phase 12) — typed capability + bounded evidence.

The desktop tool is a typed capability in front of a replaceable
DesktopBackend. It runs ONLY after the existing pipeline produced an
AuthorizedAction: it receives validated, typed arguments, dispatches to the
platform adapter, and returns honest structured ToolOutcomes with bounded
evidence. It makes no permission decisions, no task-level judgments, and no
model calls. UI/window metadata is untrusted DATA.

The ``ui_*`` operations read and act on controls through Windows UI
Automation (``desktop.uia``): by name, automation id or control type, never
by pixel. Their results are rendered as compact text in ``stdout`` so the
model can read the controls it acts on next.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.desktop.artifacts import encode_png
from nomadicos.desktop.backend import (
    DesktopBackend,
    DesktopUnavailable,
    default_backend,
)
from nomadicos.desktop.contracts import (
    MAX_CAPTURE_DIM,
    MAX_TITLE_CHARS,
    MAX_UI_TEXT,
    MAX_WINDOWS,
    FocusWindowArgs,
    ForegroundWindowArgs,
    HotkeyArgs,
    ListWindowsArgs,
    MouseClickArgs,
    MouseMoveArgs,
    MouseScrollArgs,
    PressKeyArgs,
    ScreenshotArgs,
    TypeTextArgs,
    UiFindArgs,
    UiFocusArgs,
    UiInvokeArgs,
    UiQueryArgs,
    UiSetValueArgs,
    UiTreeArgs,
    validate_point,
)
from nomadicos.desktop.uia import (
    Element,
    UiActionFailed,
    UIAutomationBackend,
    Window,
    default_uia,
    describe_query,
)
from nomadicos.kernel.errors import Failure
from nomadicos.tools.base import Tool, ToolOutcome, error_text
from nomadicos.tools.context import ExecutionContext


class DesktopTool(Tool):
    """Desktop capability in front of a replaceable backend (SPEC §3 Lego)."""

    name = "desktop"
    ops: ClassVar[dict[str, type[BaseModel]]] = {
        "screenshot": ScreenshotArgs,
        "mouse_move": MouseMoveArgs,
        "mouse_click": MouseClickArgs,
        "mouse_scroll": MouseScrollArgs,
        "type_text": TypeTextArgs,
        "press_key": PressKeyArgs,
        "hotkey": HotkeyArgs,
        "list_windows": ListWindowsArgs,
        "foreground_window": ForegroundWindowArgs,
        "focus_window": FocusWindowArgs,
        "ui_tree": UiTreeArgs,
        "ui_find": UiFindArgs,
        "ui_invoke": UiInvokeArgs,
        "ui_set_value": UiSetValueArgs,
        "ui_focus": UiFocusArgs,
    }

    def __init__(
        self,
        backend: DesktopBackend | None = None,
        uia: UIAutomationBackend | None = None,
    ) -> None:
        self.backend = backend if backend is not None else default_backend()
        self.uia = uia if uia is not None else default_uia()

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        model = self.ops.get(operation)
        if model is None:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{self.name} has no operation {operation!r}",
            )
        try:
            typed = model.model_validate(args)
        except ValidationError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"desktop.{operation} args: {error_text(exc)}",
            )
        started = time.monotonic()
        try:
            outcome = self._dispatch(operation, typed, ctx)
        except DesktopUnavailable as exc:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.RESOURCE_UNAVAILABLE,
                message=f"desktop unavailable: {exc}",
                duration_s=round(time.monotonic() - started, 3),
                evidence={"available": False},
            )
        except UiActionFailed as exc:
            # not found / disabled / unsupported: the model re-reads the UI
            # and tries again, so this is a recoverable FAILED step
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=str(exc)[:MAX_UI_TEXT],
                duration_s=round(time.monotonic() - started, 3),
            )
        except (ValueError, OSError) as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                duration_s=round(time.monotonic() - started, 3),
            )
        return outcome.model_copy(update={"duration_s": round(time.monotonic() - started, 3)})

    # ------------------------------------------------------- dispatch ---

    def _dispatch(self, operation: str, typed: BaseModel, ctx: ExecutionContext) -> ToolOutcome:
        if operation == "screenshot" and isinstance(typed, ScreenshotArgs):
            return self._screenshot(typed, ctx)
        if operation == "mouse_move" and isinstance(typed, MouseMoveArgs):
            return self._mouse_move(typed, ctx)
        if operation == "mouse_click" and isinstance(typed, MouseClickArgs):
            return self._mouse_click(typed, ctx)
        if operation == "mouse_scroll" and isinstance(typed, MouseScrollArgs):
            return self._mouse_scroll(typed, ctx)
        if operation == "type_text" and isinstance(typed, TypeTextArgs):
            return self._type_text(typed, ctx)
        if operation == "press_key" and isinstance(typed, PressKeyArgs):
            return self._press_key(typed, ctx)
        if operation == "hotkey" and isinstance(typed, HotkeyArgs):
            return self._hotkey(typed, ctx)
        if operation == "list_windows" and isinstance(typed, ListWindowsArgs):
            return self._list_windows(typed, ctx)
        if operation == "foreground_window" and isinstance(typed, ForegroundWindowArgs):
            return self._foreground_window(typed, ctx)
        if operation == "ui_tree" and isinstance(typed, UiTreeArgs):
            return self._ui_tree(typed)
        if operation == "ui_find" and isinstance(typed, UiFindArgs):
            return self._ui_find(typed)
        if operation == "ui_invoke" and isinstance(typed, UiInvokeArgs):
            return self._ui_invoke(typed)
        if operation == "ui_set_value" and isinstance(typed, UiSetValueArgs):
            return self._ui_set_value(typed)
        if operation == "ui_focus" and isinstance(typed, UiFocusArgs):
            return self._ui_focus(typed)
        assert isinstance(typed, FocusWindowArgs)
        return self._focus_window(typed, ctx)

    def _screenshot(self, a: ScreenshotArgs, ctx: ExecutionContext) -> ToolOutcome:
        width, height = self.backend.screen_size()
        if width <= 0 or height <= 0:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.RESOURCE_UNAVAILABLE,
                message=f"no usable screen reported: {width}x{height}",
            )
        if width > MAX_CAPTURE_DIM or height > MAX_CAPTURE_DIM:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"screen {width}x{height} exceeds capture bound {MAX_CAPTURE_DIM}",
            )
        raw = self.backend.capture(width, height)
        png = encode_png(width, height, raw)
        out_dir = ctx.workspace / "desktop"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"screenshot-{uuid4().hex[:12]}.png"
        path.write_bytes(png)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={
                "artifact": f"desktop/{path.name}",
                "path": str(path),
                "width": width,
                "height": height,
                "bytes": len(png),
                "sha256": hashlib.sha256(png).hexdigest(),
            },
            message=f"captured {width}x{height}",
        )

    def _mouse_move(self, a: MouseMoveArgs, ctx: ExecutionContext) -> ToolOutcome:
        width, height = self.backend.screen_size()
        try:
            validate_point(a.x, a.y, width, height)
        except ValueError as exc:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=str(exc),
                evidence={"requested": [a.x, a.y], "screen": [width, height]},
            )
        self.backend.mouse_move(a.x, a.y)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"x": a.x, "y": a.y, "screen": [width, height]},
        )

    def _mouse_click(self, a: MouseClickArgs, ctx: ExecutionContext) -> ToolOutcome:
        width, height = self.backend.screen_size()
        try:
            validate_point(a.x, a.y, width, height)
        except ValueError as exc:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=str(exc),
                evidence={"requested": [a.x, a.y], "screen": [width, height]},
            )
        self.backend.mouse_click(a.x, a.y, a.button, a.clicks)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"x": a.x, "y": a.y, "button": a.button, "clicks": a.clicks},
        )

    def _mouse_scroll(self, a: MouseScrollArgs, ctx: ExecutionContext) -> ToolOutcome:
        self.backend.mouse_scroll(a.amount)
        return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence={"amount": a.amount})

    def _type_text(self, a: TypeTextArgs, ctx: ExecutionContext) -> ToolOutcome:
        self.backend.type_text(a.text)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"typed_chars": len(a.text), "preview": a.text[:40]},
        )

    def _press_key(self, a: PressKeyArgs, ctx: ExecutionContext) -> ToolOutcome:
        self.backend.press_key(a.key)
        return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence={"keys": a.key.split("+")})

    def _hotkey(self, a: HotkeyArgs, ctx: ExecutionContext) -> ToolOutcome:
        self.backend.hotkey(a.keys)
        return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence={"keys": a.keys})

    def _list_windows(self, a: ListWindowsArgs, ctx: ExecutionContext) -> ToolOutcome:
        windows = self.backend.list_windows(a.limit)
        titles = [str(w.get("title", ""))[:MAX_TITLE_CHARS] for w in windows]
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"count": len(windows), "titles": titles[:MAX_WINDOWS]},
        )

    def _foreground_window(self, a: ForegroundWindowArgs, ctx: ExecutionContext) -> ToolOutcome:
        window = self.backend.foreground_window()
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={
                "handle": int(window.get("handle", 0) or 0),
                "title": str(window.get("title", ""))[:MAX_TITLE_CHARS],
            },
        )

    def _focus_window(self, a: FocusWindowArgs, ctx: ExecutionContext) -> ToolOutcome:
        result = self.backend.focus_window(a.title)
        focused = bool(result.get("focused"))
        evidence: dict[str, Any] = {
            "focused": focused,
            "title": str(result.get("title", ""))[:MAX_TITLE_CHARS],
        }
        if focused:
            return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence=evidence)
        reason = str(result.get("reason", "window not focused"))[:MAX_TITLE_CHARS]
        return ToolOutcome(
            status=ExecutionStatus.FAILED,
            failure=Failure.ACTION_FAILED,
            message=reason,
            evidence={**evidence, "reason": reason},
        )

    # ------------------------------------------------ UI Automation ---

    def _ui_tree(self, a: UiTreeArgs) -> ToolOutcome:
        window, elements, truncated = self.uia.tree(a.window, a.max_depth, a.max_nodes)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout=render_ui(window, elements, truncated=truncated),
            evidence={
                "title": str(window.get("title", ""))[:MAX_TITLE_CHARS],
                "window": window,
                "count": len(elements),
                "truncated": truncated,
                "elements": elements,
            },
        )

    def _ui_find(self, a: UiFindArgs) -> ToolOutcome:
        window, elements = self.uia.find(a, a.limit)
        evidence = {
            "title": str(window.get("title", ""))[:MAX_TITLE_CHARS],
            "window": window,
            "query": _query(a),
            "count": len(elements),
            "elements": elements,
        }
        if not elements:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"no control matches {describe_query(a)}",
                evidence=evidence,
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout=render_ui(window, elements, truncated=False),
            evidence=evidence,
        )

    def _ui_acted(
        self, action: str, a: UiQueryArgs, window: Window, element: Element | None, **extra: Any
    ) -> ToolOutcome:
        title = str(window.get("title", ""))[:MAX_TITLE_CHARS]
        shown = render_ui(window, [element], truncated=False) if element else "(control gone)"
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout=f"{action} done. {shown}",
            evidence={"title": title, "query": _query(a), "element": element, **extra},
        )

    def _ui_invoke(self, a: UiInvokeArgs) -> ToolOutcome:
        window, element, via = self.uia.invoke(a)
        return self._ui_acted("invoke", a, window, element, via=via)

    def _ui_set_value(self, a: UiSetValueArgs) -> ToolOutcome:
        window, element = self.uia.set_value(a, a.value)
        return self._ui_acted("set_value", a, window, element)

    def _ui_focus(self, a: UiFocusArgs) -> ToolOutcome:
        window, element = self.uia.focus(a)
        return self._ui_acted("focus", a, window, element)


#: budget for the text rendering the model reads (one line per control)
MAX_UI_RENDER_CHARS = 1800


def _query(a: UiQueryArgs) -> dict[str, Any]:
    keep = ("window", "name", "automation_id", "control_type", "index")
    return {k: v for k, v in a.model_dump().items() if k in keep and v is not None}


def render_ui(window: Window, elements: list[Element], *, truncated: bool) -> str:
    """Compact, bounded text view of controls: what the model reads. Every
    name and value is untrusted DATA from another application."""
    lines = [f'window "{str(window.get("title", ""))[:80]}"']
    for e in elements:
        indent = "  " * min(int(e.get("depth", 1)), 8)
        parts = [f'{indent}{e.get("control_type", "?")} "{str(e.get("name", ""))[:60]}"']
        if e.get("automation_id"):
            parts.append(f"id={str(e['automation_id'])[:40]}")
        if e.get("actions"):
            parts.append("[" + ",".join(e["actions"]) + "]")
        if e.get("password"):
            parts.append("value=<password>")
        elif "value" in e:
            parts.append(f'value="{str(e["value"])[:60]}"')
        if "toggle" in e:
            parts.append(f"toggle={e['toggle']}")
        if e.get("enabled") is False:
            parts.append("disabled")
        lines.append(" ".join(parts))
    if truncated:
        lines.append("(truncated: narrow with ui_find or a smaller max_depth)")
    text = "\n".join(lines)
    if len(text) > MAX_UI_RENDER_CHARS:
        text = text[: MAX_UI_RENDER_CHARS - 12].rsplit("\n", 1)[0] + "\n(cut short)"
    return text
