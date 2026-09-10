"""Screenshot providers: fake (CI) + real (Windows-first, lazy PIL import).

The real provider is hardware/OS-dependent and is exercised only under
`@pytest.mark.hardware`; the fake provider covers the full pipeline in CI.
"""

import asyncio
from typing import Any

from nomadicos.core.logging import get_logger
from nomadicos.vision.base import CapturedFrame, ScreenshotProvider

logger = get_logger("vision.screenshot")


class FakeScreenshotProvider(ScreenshotProvider):
    """Deterministic captures for tests: scripted byte payloads."""

    def __init__(
        self, frames: list[bytes] | None = None, *, width: int = 800, height: int = 600
    ) -> None:
        self._frames = list(frames or [b"fake-screen-v1"])
        self._width = width
        self._height = height
        self.capture_calls = 0
        self._index = 0

    def queue_frame(self, payload: bytes) -> None:
        self._frames.append(payload)

    def capture(self) -> CapturedFrame:
        self.capture_calls += 1
        payload = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return CapturedFrame.create(payload, self._width, self._height)


class PillowScreenshotProvider(ScreenshotProvider):
    """Real screen capture via Pillow's ImageGrab (Windows-first, BP §173).

    Lazy import keeps CI green without display/PIL. Hardware-only usage.
    """

    def __init__(self, *, monitor: int = 1) -> None:
        self._monitor = monitor

    def capture(self) -> CapturedFrame:
        try:
            from PIL import ImageGrab
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Pillow is required for real screen capture (pip install Pillow)"
            ) from exc

        async def _grab() -> Any:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: ImageGrab.grab(all_screens=self._monitor != 1)
            )

        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Inside an event loop the caller should use capture_async(); this
            # sync path is for direct hardware testing.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                image = pool.submit(lambda: ImageGrab.grab()).result(timeout=10)
        else:
            image = ImageGrab.grab()
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return CapturedFrame.create(buffer.getvalue(), image.width, image.height)


__all__ = ["FakeScreenshotProvider", "PillowScreenshotProvider"]
