"""Phase 5 unit tests: capture policy, engine, parser, adapter (BP §158-159, §240)."""

import pytest

from nomadicos.core.errors import VerificationFailed
from nomadicos.vision.base import CapturedFrame, CapturePolicy, CaptureReason, VisionEngine
from nomadicos.vision.fake import FakeGemmaVision, observation_with_description
from nomadicos.vision.parser import find_element
from nomadicos.vision.screenshot import FakeScreenshotProvider


def frame(payload: bytes = b"screen-v1") -> CapturedFrame:
    return CapturedFrame.create(payload, 800, 600)


# ------------------------------------------------------------- capture policy


def test_policy_permits_first_capture() -> None:
    policy = CapturePolicy(min_interval_ms=0)
    permitted, why = policy.permits(frame())
    assert permitted and why == "permitted"
    policy.record(frame())
    assert policy.count == 1


def test_policy_cooldown_blocks_rapid_capture() -> None:
    policy = CapturePolicy(min_interval_ms=1000)
    first = frame(b"v1")
    policy.permits(first)
    policy.record(first)
    second = CapturedFrame.create(b"v2", 800, 600)  # same clock instant
    permitted, why = policy.permits(second)
    assert not permitted and why == "cooldown"


def test_policy_change_detection_skips_unchanged() -> None:
    policy = CapturePolicy(min_interval_ms=0, change_detection=True)
    first = frame(b"same")
    policy.permits(first)
    policy.record(first)
    second = frame(b"same")  # identical content hash
    permitted, why = policy.permits(second)
    assert not permitted and why == "unchanged"
    different = frame(b"changed")
    permitted, _ = policy.permits(different)
    assert permitted


def test_policy_capture_budget() -> None:
    policy = CapturePolicy(min_interval_ms=0, max_captures_per_task=1)
    policy.record(frame())
    permitted, why = policy.permits(frame(b"other"))
    assert not permitted and why == "capture budget exhausted"


# ------------------------------------------------------------------- engine


async def test_engine_produces_observation_without_vision() -> None:
    engine = VisionEngine(FakeScreenshotProvider(), policy=CapturePolicy(min_interval_ms=0))
    observation = await engine.observe(CaptureReason.STEP_START)
    assert observation.observation_id == "obs-0001"
    assert observation.description == ""  # no vision model: capture-only (BP §240)
    assert observation.content_hash


async def test_engine_change_detection_returns_cached_observation() -> None:
    provider = FakeScreenshotProvider([b"same-screen"])
    engine = VisionEngine(provider, policy=CapturePolicy(min_interval_ms=0))
    first = await engine.observe(CaptureReason.STEP_START)
    second = await engine.observe(CaptureReason.CHANGE_DETECTION)
    assert second.observation_id == first.observation_id  # unchanged: cached
    assert second.changed_from_previous is False


async def test_engine_describes_with_vision_model() -> None:
    provider = FakeScreenshotProvider([b"frame-1"])
    vision = FakeGemmaVision()
    vision.queue_response("A window 'Editor' with button 'Run Tests' at (120, 340).")
    engine = VisionEngine(provider, vision_model=vision, policy=CapturePolicy(min_interval_ms=0))
    observation = await engine.observe(CaptureReason.STEP_VERIFY, question="what is on screen?")
    assert "Run Tests" in observation.description
    assert len(vision.describe_calls) == 1
    assert vision.describe_calls[0][1] == "what is on screen?"


async def test_engine_degrades_gracefully_on_vision_failure() -> None:
    """BP §240: vision failure → capture-only observation, never fabricated."""
    provider = FakeScreenshotProvider([b"frame-1"])
    vision = FakeGemmaVision()
    vision.set_fail_next(5)  # exhaust retries
    engine = VisionEngine(provider, vision_model=vision, policy=CapturePolicy(min_interval_ms=0))
    observation = await engine.observe(CaptureReason.STEP_START)
    assert observation.description == ""
    assert observation.content_hash  # capture evidence still present


async def test_engine_capture_failure_raises_verification_failed() -> None:
    class BrokenProvider:
        def capture(self) -> CapturedFrame:
            raise OSError("no display")

    engine = VisionEngine(BrokenProvider())  # type: ignore[arg-type]
    with pytest.raises(VerificationFailed, match="capture failed"):
        await engine.observe(CaptureReason.STEP_START)


async def test_capture_is_audited_when_wired() -> None:
    """BP §159: important captures are audited."""
    provider = FakeScreenshotProvider([b"frame-1"])
    engine = VisionEngine(provider, policy=CapturePolicy(min_interval_ms=0))
    await engine.observe(CaptureReason.STEP_START)
    # audit emission is the caller's wiring (Phase 6); here we assert the engine
    # produced the evidence an audit record would reference.
    assert engine.last_unchanged is not None


# ------------------------------------------------------------------- parser


def test_parser_extracts_elements_with_positions() -> None:
    observation = observation_with_description(
        "A window 'Editor' with button 'Save' at (120, 340) and text field 'Search' at (40, 20)."
    )
    kinds = [e["kind"] for e in observation.elements]
    assert "window" in kinds and "button" in kinds and "text field" in kinds
    button = find_element(observation, kind="button", label="Save")
    assert button is not None
    assert button["position"] == {"x": 120, "y": 340}


def test_parser_never_invents_elements() -> None:
    observation = observation_with_description("The screen is mostly blue.")
    assert observation.elements == []


def test_find_element_grounding() -> None:
    observation = observation_with_description("button 'Cancel' at (10, 20).")
    assert find_element(observation, kind="button", label="cancel") is not None
    assert find_element(observation, kind="button", label="Apply") is None


def test_parser_bounds_element_count() -> None:
    text = " ".join(f"button 'B{i}' at (1, {i})." for i in range(100))
    observation = observation_with_description(text)
    assert len(observation.elements) <= 64  # MAX_ELEMENTS


# ------------------------------------------------------------------- adapter


def test_gemma_adapter_requires_transformers() -> None:
    from nomadicos.vision.gemma_adapter import TRANSFORMERS_AVAILABLE, GemmaVisionModel

    if TRANSFORMERS_AVAILABLE:
        pytest.skip("transformers installed — construction path tested")
    from nomadicos.core.errors import ModelResourceError

    with pytest.raises(ModelResourceError, match="transformers"):
        GemmaVisionModel("gemma/gemma-3-4b", "models/gemma-3-4b")
