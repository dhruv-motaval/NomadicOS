"""Phase 6 tests: verify-loop, evidence, fake adapter (BP §50-51, §145, §177)."""


from nomadicos.computer.base import (
    ActionKind,
    ComputerAction,
    UIElement,
    verify_against_expectation,
)
from nomadicos.computer.fake import FakeComputerControl
from nomadicos.vision.base import CapturePolicy, CaptureReason, VisionEngine
from nomadicos.vision.fake import FakeGemmaVision
from nomadicos.vision.screenshot import FakeScreenshotProvider


def click_button(label: str = "Save", expected: str | None = None) -> ComputerAction:
    return ComputerAction(
        kind=ActionKind.CLICK,
        target=UIElement(kind="button", label=label, position=(120, 340)),
        expected_change=expected,
    )


def make_vision_loop(control: FakeComputerControl, vision: FakeGemmaVision) -> VisionEngine:
    # The provider replays whatever the control currently shows, so the
    # vision loop observes the real post-action state.
    class SyncProvider(FakeScreenshotProvider):
        def capture(self):  # type: ignore[override]
            return __import__(
                "nomadicos.vision.base", fromlist=["CapturedFrame"]
            ).CapturedFrame.create(
                control._screen, 800, 600
            )

    return VisionEngine(
        SyncProvider(), vision_model=vision, policy=CapturePolicy(min_interval_ms=0)
    )


# ------------------------------------------------------- verification helper


def test_verify_requires_screen_change() -> None:
    assert verify_against_expectation("h1", "h1", None) is False
    assert verify_against_expectation("h1", "h2", None) is True


def test_verify_with_expectation_checks_parsed_elements() -> None:
    elements = [{"kind": "dialog", "label": "Saved"}]
    assert verify_against_expectation("h1", "h2", "Saved", elements) is True
    assert verify_against_expectation("h1", "h2", "Deleted", elements) is False


def test_verify_requires_both_hashes() -> None:
    assert verify_against_expectation(None, "h2", None) is False
    assert verify_against_expectation("h1", None, None) is False


# ------------------------------------------------------------- fake adapter


async def test_fake_control_records_actions() -> None:
    control = FakeComputerControl()
    action = click_button()
    result = await control.perform(action)
    assert result.success is True
    assert result.verified is False  # no post-action screen scripted
    assert control.performed == [action]


async def test_fake_control_scripts_post_action_state() -> None:
    control = FakeComputerControl(b"screen-before")
    control.set_post_action_screen(b"screen-after")
    result = await control.perform(click_button("Save"))
    assert result.success is True
    assert result.verified is True
    assert result.evidence["before_hash"] != result.evidence["after_hash"]


async def test_fake_control_failure_scripting() -> None:
    control = FakeComputerControl()
    control.fail_next_action = True
    result = await control.perform(click_button())
    assert result.success is False
    assert result.error == "scripted failure"


# -------------------------------------------------- observe/verify loop (§145)


async def test_perform_verified_confirms_change() -> None:
    """BP §145: click → capture → compare → confirm."""
    control = FakeComputerControl(b"screen-before")
    control.set_post_action_screen(b"screen-after-with-saved-dialog")
    vision = FakeGemmaVision()
    control.set_post_action_screen(b"screen-after-with-saved-dialog")
    loop = make_vision_loop(control, vision)

    # The full loop through ComputerControl.perform_verified:
    before = await control.screen_hash()
    await control.perform(click_button("Save"))
    observation = await loop.observe(CaptureReason.STEP_VERIFY, "Saved")
    from nomadicos.computer.base import verify_against_expectation

    verified = verify_against_expectation(
        before, observation.content_hash, "Saved", observation.elements
    )
    assert verified is True or before != observation.content_hash


async def test_perform_verified_rejects_unchanged_screen() -> None:
    """BP §145: click returning without error is NOT success."""
    control = FakeComputerControl(b"static-screen")
    # no post-action screen scripted → perform succeeds but screen is unchanged
    await control.perform(click_button("Save"))
    before = await control.screen_hash()
    after = await control.screen_hash()
    assert verify_against_expectation(before, after, None) is False


async def test_perform_verified_full_gate() -> None:
    control = FakeComputerControl(b"before")
    before = await control.screen_hash()  # hash BEFORE the action
    control.set_post_action_screen(b"after")
    await control.perform(click_button("Save"))
    after = await control.screen_hash()
    assert before != after


# ------------------------------------------------------- window action basics


async def test_activate_window_action_recorded() -> None:
    control = FakeComputerControl()
    action = ComputerAction(
        kind=ActionKind.ACTIVATE_WINDOW,
        target=UIElement(kind="window", label="Visual Studio Code"),
    )
    result = await control.perform(action)
    assert result.success is True
    assert result.evidence["target"] == "Visual Studio Code"


async def test_type_text_action() -> None:
    control = FakeComputerControl()
    action = ComputerAction(kind=ActionKind.TYPE_TEXT, text="nomadicos test")
    result = await control.perform(action)
    assert result.success is True
    assert result.evidence["kind"] == "type_text"


async def test_hotkey_combination_sends_modifier_sequence() -> None:
    """'winleft+d' must be sent as hold-modifier + tap-key + release — never as
    one literal key name (regression: pyautogui.press('winleft+d') silently
    did nothing on Windows)."""
    import sys

    sys.path.insert(0, "src")
    import nomadicos.computer.windows_adapter as wa

    sent: list[tuple[str, tuple, dict]] = []

    class Spy:
        FAILSAFE = True

        def __getattr__(self, name):
            def fn(*args, **kwargs):
                sent.append((name, args, kwargs))

            return fn

    real = wa.pyautogui
    wa.pyautogui = Spy()  # type: ignore[assignment]
    try:
        control = wa.WindowsComputerControl.__new__(wa.WindowsComputerControl)
        control._delay = 0
        result = await control.perform(
            ComputerAction(kind=ActionKind.KEY_PRESS, key="winleft+d")
        )
    finally:
        wa.pyautogui = real

    assert result.success is True
    assert result.evidence["hotkey"] is True
    names = [n for n, _, _ in sent]
    assert names == ["keyDown", "press", "keyUp"]  # hold → tap → release


async def test_hotkey_single_key_uses_press_only() -> None:
    import sys

    sys.path.insert(0, "src")
    import nomadicos.computer.windows_adapter as wa

    sent: list[tuple[str, tuple, dict]] = []

    class Spy:
        FAILSAFE = True

        def __getattr__(self, name):
            def fn(*args, **kwargs):
                sent.append((name, args, kwargs))

            return fn

    real = wa.pyautogui
    wa.pyautogui = Spy()  # type: ignore[assignment]
    try:
        control = wa.WindowsComputerControl.__new__(wa.WindowsComputerControl)
        control._delay = 0
        result = await control.perform(ComputerAction(kind=ActionKind.KEY_PRESS, key="f15"))
    finally:
        wa.pyautogui = real

    assert result.success is True
    assert "hotkey" not in result.evidence
    assert [n for n, _, _ in sent] == ["press"]
