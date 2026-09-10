"""Vision Runtime (BP §9, §51, §158-159, §172-177, §240; ADR-0004).

Local-only perception: capture → local Gemma-family vision → structured
observation. Event-driven capture only (BP §158); screen data never leaves the
machine (BP §159, I11).
"""

from nomadicos.vision.base import (
    CapturePolicy,
    CaptureReason,
    ScreenObservation,
    ScreenshotProvider,
    VisionEngine,
)
from nomadicos.vision.fake import FakeScreenshotProvider
from nomadicos.vision.parser import ObservationParser, parse_observation

__all__ = [
    "CapturePolicy",
    "CaptureReason",
    "FakeScreenshotProvider",
    "ObservationParser",
    "ScreenObservation",
    "ScreenshotProvider",
    "VisionEngine",
    "parse_observation",
]
