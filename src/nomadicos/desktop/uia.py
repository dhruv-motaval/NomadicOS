"""Windows UI Automation adapter (SPEC §26, §53 Phase 12): the screen as data.

UI Automation exposes every accessible control as a typed element: its
control type, name, automation id, bounds, state and the actions it
supports. Acting on an element ("invoke the button named Save in Notepad")
instead of a pixel coordinate is exact, needs no model, and survives the
window moving, which is what makes a recorded step replayable.

Like ``DesktopBackend``, the adapter is DATA/CONTROL only: no authority, no
policy, no task judgment. Every name, value and title it returns is
untrusted DATA (SPEC §7). Password fields never report their value.

``ComtypesUIA`` talks to UIAutomationCore through comtypes (the ``desktop``
extra); ``FakeUIA`` is the deterministic in-memory adapter for tests;
``UnavailableUIA`` reports honest absence elsewhere.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any, NoReturn

from nomadicos.desktop.backend import DesktopUnavailable
from nomadicos.desktop.contracts import (
    CONTROL_TYPE_NAMES,
    CONTROL_TYPES,
    MAX_TITLE_CHARS,
    MAX_UI_TEXT,
    MAX_UI_VALUE,
    UiQueryArgs,
    UiTargetArgs,
)

Element = dict[str, Any]
Window = dict[str, Any]

_TOGGLE_STATES = {0: "off", 1: "on", 2: "indeterminate"}


class UiActionFailed(Exception):
    """The control was not found, is disabled, or does not support the
    action: a recoverable FAILED step, never a tool crash."""


def describe_query(q: UiQueryArgs) -> str:
    parts = [
        f"{key}={value!r}"
        for key, value in (
            ("window", q.window),
            ("name", q.name),
            ("automation_id", q.automation_id),
            ("control_type", q.control_type),
        )
        if value
    ]
    return " ".join(parts)


class UIAutomationBackend:
    """UIA adapter surface used by the desktop tool."""

    def tree(
        self, window: str | None, max_depth: int, max_nodes: int
    ) -> tuple[Window, list[Element], bool]:  # pragma: no cover - protocol
        """(window, elements in depth-first order, truncated?)"""
        raise NotImplementedError

    def find(
        self, query: UiQueryArgs, limit: int
    ) -> tuple[Window, list[Element]]:  # pragma: no cover - protocol
        raise NotImplementedError

    def invoke(
        self, target: UiTargetArgs
    ) -> tuple[Window, Element | None, str]:  # pragma: no cover - protocol
        """(window, element after the action or None if it vanished, via)"""
        raise NotImplementedError

    def set_value(
        self, target: UiTargetArgs, value: str
    ) -> tuple[Window, Element | None]:  # pragma: no cover - protocol
        raise NotImplementedError

    def focus(self, target: UiTargetArgs) -> tuple[Window, Element | None]:  # pragma: no cover
        raise NotImplementedError


class UnavailableUIA(UIAutomationBackend):
    def __init__(self, reason: str = "UI Automation is Windows-only") -> None:
        self.reason = reason

    def _refuse(self) -> NoReturn:
        raise DesktopUnavailable(self.reason)

    def tree(self, window: str | None, max_depth: int, max_nodes: int) -> NoReturn:
        self._refuse()

    def find(self, query: UiQueryArgs, limit: int) -> NoReturn:
        self._refuse()

    def invoke(self, target: UiTargetArgs) -> NoReturn:
        self._refuse()

    def set_value(self, target: UiTargetArgs, value: str) -> NoReturn:
        self._refuse()

    def focus(self, target: UiTargetArgs) -> NoReturn:
        self._refuse()


def default_uia() -> UIAutomationBackend:
    """Platform-selected adapter; comtypes loads lazily on first use."""
    import sys

    if sys.platform != "win32":
        return UnavailableUIA()
    try:
        import comtypes  # noqa: F401
    except ImportError:
        return UnavailableUIA("UI Automation needs the 'desktop' extra (comtypes)")
    return ComtypesUIA()


# ------------------------------------------------------------- fake -----


class FakeUIA(UIAutomationBackend):
    """Deterministic in-memory adapter (SPEC §10 fake adapters).

    ``windows`` maps a title to its elements (dicts in the evidence shape,
    each with ``actions``); the first window is the foreground one.
    Actions are recorded in ``log``; ``set_value`` updates the element.
    """

    def __init__(self, windows: dict[str, list[Element]] | None = None) -> None:
        self.windows: dict[str, list[Element]] = {
            title: [dict(e) for e in elements] for title, elements in (windows or {}).items()
        }
        self.log: list[tuple[str, str, Any]] = []

    def _window(self, title: str | None) -> tuple[str, list[Element]]:
        if not self.windows:
            raise UiActionFailed("no windows")
        if title is None:
            first = next(iter(self.windows))
            return first, self.windows[first]
        for name, elements in self.windows.items():
            if title.casefold() in name.casefold():
                return name, elements
        raise UiActionFailed(f"no window whose title contains {title!r}")

    @staticmethod
    def _matches(q: UiQueryArgs, e: Element) -> bool:
        return (
            (q.name is None or str(e.get("name", "")).casefold() == q.name.casefold())
            and (q.automation_id is None or e.get("automation_id") == q.automation_id)
            and (q.control_type is None or e.get("control_type") == q.control_type)
        )

    def tree(
        self, window: str | None, max_depth: int, max_nodes: int
    ) -> tuple[Window, list[Element], bool]:
        title, elements = self._window(window)
        shown = [dict(e) for e in elements if int(e.get("depth", 0)) <= max_depth]
        return {"title": title}, shown[:max_nodes], len(shown) > max_nodes

    def find(self, query: UiQueryArgs, limit: int) -> tuple[Window, list[Element]]:
        title, elements = self._window(query.window)
        return {"title": title}, [dict(e) for e in elements if self._matches(query, e)][:limit]

    def _target(self, t: UiTargetArgs) -> tuple[str, Element]:
        title, elements = self._window(t.window)
        found = [e for e in elements if self._matches(t, e)]
        if len(found) <= t.index:
            raise UiActionFailed(f"no control matches {describe_query(t)} (found {len(found)})")
        return title, found[t.index]

    def _require(self, e: Element, action: str) -> None:
        if not e.get("enabled", True):
            raise UiActionFailed("control is disabled")
        if action not in e.get("actions", []):
            raise UiActionFailed(f"control does not support {action}")

    def invoke(self, target: UiTargetArgs) -> tuple[Window, Element | None, str]:
        title, e = self._target(target)
        self._require(e, "invoke")
        self.log.append(("invoke", title, e.get("name")))
        return {"title": title}, dict(e), "invoke"

    def set_value(self, target: UiTargetArgs, value: str) -> tuple[Window, Element | None]:
        title, e = self._target(target)
        self._require(e, "set_value")
        e["value"] = value
        self.log.append(("set_value", title, value))
        return {"title": title}, dict(e)

    def focus(self, target: UiTargetArgs) -> tuple[Window, Element | None]:
        title, e = self._target(target)
        self._require(e, "focus")
        self.log.append(("focus", title, e.get("name")))
        return {"title": title}, dict(e)


# --------------------------------------------------------- comtypes -----


class ComtypesUIA(UIAutomationBackend):
    """UIAutomationCore through comtypes. COM is per thread, so the automation
    object is created lazily for each thread that uses it."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _api(self) -> tuple[Any, Any]:
        api = getattr(self._local, "api", None)
        if api is not None:
            return api
        try:
            import comtypes
            import comtypes.client

            try:
                comtypes.CoInitialize()
            except OSError:
                pass  # already initialized for this thread
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia_lib

            automation = comtypes.client.CreateObject(
                uia_lib.CUIAutomation, interface=uia_lib.IUIAutomation
            )
        except Exception as exc:  # noqa: BLE001 - any COM/import failure = absence
            raise DesktopUnavailable(f"UI Automation unavailable: {exc}") from exc
        api = (uia_lib, automation)
        self._local.api = api
        return api

    # ------------------------------------------------------- helpers ---

    def _window(self, title: str | None) -> Any:
        lib, automation = self._api()
        if title is None:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                raise UiActionFailed("no foreground window")
            return automation.ElementFromHandle(hwnd)
        root = automation.GetRootElement()
        children = root.FindAll(lib.TreeScope_Children, automation.CreateTrueCondition())
        needle = title.casefold()
        for i in range(children.Length):
            element = children.GetElement(i)
            if needle in (element.CurrentName or "").casefold():
                return element
        raise UiActionFailed(f"no window whose title contains {title!r}")

    @staticmethod
    def _window_info(window: Any) -> Window:
        return {
            "title": (window.CurrentName or "")[:MAX_TITLE_CHARS],
            "handle": int(window.CurrentNativeWindowHandle or 0),
            "process_id": int(window.CurrentProcessId or 0),
        }

    def _pattern(self, element: Any, pattern_id: int, interface: Any) -> Any:
        try:
            unknown = element.GetCurrentPattern(pattern_id)
        except Exception:  # noqa: BLE001 - COMError: pattern not available
            return None
        if not unknown:
            return None
        return unknown.QueryInterface(interface)

    def _describe(self, element: Any, depth: int | None = None) -> Element:
        lib, _ = self._api()
        rect = element.CurrentBoundingRectangle
        control = int(element.CurrentControlType)
        info: Element = {
            "control_type": CONTROL_TYPE_NAMES.get(control, str(control)),
            "name": (element.CurrentName or "")[:MAX_UI_TEXT],
            "automation_id": (element.CurrentAutomationId or "")[:MAX_UI_TEXT],
            "class_name": (element.CurrentClassName or "")[:MAX_UI_TEXT],
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "enabled": bool(element.CurrentIsEnabled),
        }
        actions: list[str] = []
        if self._pattern(element, lib.UIA_InvokePatternId, lib.IUIAutomationInvokePattern):
            actions.append("invoke")
        value = self._pattern(element, lib.UIA_ValuePatternId, lib.IUIAutomationValuePattern)
        if value is not None:
            if element.CurrentIsPassword:
                info["password"] = True  # never read a password's value
            else:
                info["value"] = (value.CurrentValue or "")[:MAX_UI_VALUE]
            if not value.CurrentIsReadOnly:
                actions.append("set_value")
        toggle = self._pattern(element, lib.UIA_TogglePatternId, lib.IUIAutomationTogglePattern)
        if toggle is not None:
            info["toggle"] = _TOGGLE_STATES.get(int(toggle.CurrentToggleState), "unknown")
            actions.append("invoke")
        if element.CurrentIsKeyboardFocusable:
            actions.append("focus")
        info["actions"] = sorted(set(actions))
        if depth is not None:
            info["depth"] = depth
        return info

    def _describe_after(self, element: Any) -> Element | None:
        try:
            return self._describe(element)
        except Exception:  # noqa: BLE001 - the control may be gone (dialog closed)
            return None

    def _matches(self, query: UiQueryArgs, limit: int) -> tuple[Any, list[Any]]:
        lib, automation = self._api()
        window = self._window(query.window)
        conditions = []
        if query.name:
            conditions.append(
                automation.CreatePropertyConditionEx(
                    lib.UIA_NamePropertyId, query.name, lib.PropertyConditionFlags_IgnoreCase
                )
            )
        if query.automation_id:
            conditions.append(
                automation.CreatePropertyCondition(
                    lib.UIA_AutomationIdPropertyId, query.automation_id
                )
            )
        if query.control_type:
            conditions.append(
                automation.CreatePropertyCondition(
                    lib.UIA_ControlTypePropertyId, CONTROL_TYPES[query.control_type]
                )
            )
        condition = conditions[0]
        for extra in conditions[1:]:
            condition = automation.CreateAndCondition(condition, extra)
        found = window.FindAll(lib.TreeScope_Subtree, condition)
        return window, [found.GetElement(i) for i in range(min(found.Length, limit))]

    def _target(self, target: UiTargetArgs) -> tuple[Any, Any]:
        window, found = self._matches(target, target.index + 1)
        if len(found) <= target.index:
            raise UiActionFailed(
                f"no control matches {describe_query(target)} (found {len(found)})"
            )
        element = found[target.index]
        if not element.CurrentIsEnabled:
            raise UiActionFailed("control is disabled")
        return window, element

    def _com(self, call: Any) -> Any:
        """COM errors from stale or vanished elements are failed steps."""
        try:
            return call()
        except (UiActionFailed, DesktopUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - COMError has no stable base
            raise UiActionFailed(f"UI Automation call failed: {exc}") from exc

    # ------------------------------------------------------- surface ---

    def tree(
        self, window: str | None, max_depth: int, max_nodes: int
    ) -> tuple[Window, list[Element], bool]:
        def walk() -> tuple[Window, list[Element], bool]:
            _, automation = self._api()
            root = self._window(window)
            walker = automation.ControlViewWalker
            out: list[Element] = []
            stack: list[tuple[Any, int]] = [(root, 0)]
            while stack:
                if len(out) >= max_nodes:
                    return self._window_info(root), out, True
                element, depth = stack.pop()
                out.append(self._describe(element, depth))
                if depth >= max_depth:
                    continue
                children: list[Any] = []
                child = walker.GetFirstChildElement(element)
                while child and len(children) < max_nodes:
                    children.append(child)
                    child = walker.GetNextSiblingElement(child)
                stack.extend((c, depth + 1) for c in reversed(children))
            return self._window_info(root), out, False

        return self._com(walk)

    def find(self, query: UiQueryArgs, limit: int) -> tuple[Window, list[Element]]:
        def run() -> tuple[Window, list[Element]]:
            window, found = self._matches(query, limit)
            return self._window_info(window), [self._describe(e) for e in found]

        return self._com(run)

    def invoke(self, target: UiTargetArgs) -> tuple[Window, Element | None, str]:
        def run() -> tuple[Window, Element | None, str]:
            lib, _ = self._api()
            window, element = self._target(target)
            info = self._window_info(window)
            invoke = self._pattern(element, lib.UIA_InvokePatternId, lib.IUIAutomationInvokePattern)
            if invoke is not None:
                invoke.Invoke()
                return info, self._describe_after(element), "invoke"
            toggle = self._pattern(element, lib.UIA_TogglePatternId, lib.IUIAutomationTogglePattern)
            if toggle is not None:
                toggle.Toggle()
                return info, self._describe_after(element), "toggle"
            raise UiActionFailed("control supports neither invoke nor toggle")

        return self._com(run)

    def set_value(self, target: UiTargetArgs, value: str) -> tuple[Window, Element | None]:
        def run() -> tuple[Window, Element | None]:
            lib, _ = self._api()
            window, element = self._target(target)
            pattern = self._pattern(element, lib.UIA_ValuePatternId, lib.IUIAutomationValuePattern)
            if pattern is None or pattern.CurrentIsReadOnly:
                raise UiActionFailed("control has no writable value")
            pattern.SetValue(value)
            return self._window_info(window), self._describe_after(element)

        return self._com(run)

    def focus(self, target: UiTargetArgs) -> tuple[Window, Element | None]:
        def run() -> tuple[Window, Element | None]:
            window, element = self._target(target)
            element.SetFocus()
            return self._window_info(window), self._describe_after(element)

        return self._com(run)
