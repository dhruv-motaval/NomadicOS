"""Vision contracts: capture policy, observations, engine (BP §158-159).

- BP §158: never capture at maximum frequency by default — step-based and
  change-detection capture only.
- BP §159: capture stays local, short retention, audited for important tasks.
- BP §240: vision failure → structured fallback → retry with limits → ask user.
- ADR-0004: VisionModel is a capability; the engine asks for the capability.
"""

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nomadicos.core.errors import VerificationFailed


class CaptureReason(StrEnum):
    """Why a screenshot was taken (BP §158: event/step/change driven)."""

    STEP_START = "step_start"
    STEP_VERIFY = "step_verify"
    CHANGE_DETECTION = "change_detection"
    EXPLICIT_REQUEST = "explicit_request"


class ScreenObservation(BaseModel):
    """Structured observation of one capture (BP §51, §175)."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1, max_length=64)
    captured_at_ms: float = Field(ge=0)
    content_hash: str = Field(min_length=8, max_length=64)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    description: str = ""  # local vision model output (or "capture-only")
    elements: list[dict[str, Any]] = Field(default_factory=list)
    # BP §175: accessibility/DOM metadata can enrich the observation without vision
    structured_state: dict[str, Any] = Field(default_factory=dict)
    changed_from_previous: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.elements and not self.structured_state


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One raw capture. image_bytes never leave the process unredacted (I11)."""

    image_bytes: bytes
    width: int
    height: int
    captured_at_ms: float
    content_hash: str

    @classmethod
    def create(cls, image_bytes: bytes, width: int, height: int) -> "CapturedFrame":
        return cls(
            image_bytes=image_bytes,
            width=width,
            height=height,
            captured_at_ms=time.monotonic() * 1000,
            content_hash=hashlib.sha256(image_bytes).hexdigest()[:16],
        )


class CapturePolicy:
    """BP §158: decides WHEN capture is permitted.

    - step-based: capture at explicit step boundaries (default)
    - change-detection: capture only when the screen content hash differs
    - cooldown: minimum interval between captures (throttle)
    """

    def __init__(
        self,
        *,
        min_interval_ms: float = 250.0,
        change_detection: bool = True,
        max_captures_per_task: int = 200,
    ) -> None:
        self.min_interval_ms = min_interval_ms
        self.change_detection = change_detection
        self.max_captures_per_task = max_captures_per_task
        self._last_capture_ms: float = 0.0
        self._last_hash: str | None = None
        self._count = 0

    def permits(self, frame: CapturedFrame) -> tuple[bool, str]:
        """Returns (permitted, reason). Unchanged screens are skipped when
        change-detection is on (BP §158)."""
        if self._count >= self.max_captures_per_task:
            return False, "capture budget exhausted"
        elapsed = frame.captured_at_ms - self._last_capture_ms
        if elapsed < self.min_interval_ms:
            return False, "cooldown"
        if (
            self.change_detection
            and self._last_hash is not None
            and frame.content_hash == self._last_hash
        ):
            return False, "unchanged"
        return True, "permitted"

    @property
    def last_hash(self) -> str | None:
        return self._last_hash

    def record(self, frame: CapturedFrame) -> None:
        self._last_capture_ms = frame.captured_at_ms
        self._last_hash = frame.content_hash
        self._count += 1

    @property
    def count(self) -> int:
        return self._count


class ScreenshotProvider(ABC):
    """Capture abstraction (BP §172 OS adapters; Windows first per §173)."""

    @abstractmethod
    def capture(self) -> CapturedFrame: ...


class ScreenStore(ABC):
    """Retention-bounded local storage for captures (BP §159)."""

    @abstractmethod
    def store(self, frame: CapturedFrame, reason: CaptureReason) -> str: ...

    @abstractmethod
    def evict_expired(self, retention_seconds: float) -> int: ...


class VisionEngine:
    """Orchestrates capture → policy → optional vision → observation (BP §51).

    Failure semantics (BP §240): a vision error degrades to a capture-only
    observation (structured facts without description) — never a fabricated
    description, never a blind action.
    """

    def __init__(
        self,
        provider: ScreenshotProvider,
        *,
        policy: CapturePolicy | None = None,
        vision_model: Any | None = None,  # VisionModel capability (ADR-0004)
        store: ScreenStore | None = None,
        max_retries: int = 2,
    ) -> None:
        self._provider = provider
        self._policy = policy or CapturePolicy()
        self._vision_model = vision_model
        self._store = store
        self._max_retries = max_retries
        self._sequence = 0
        self.last_unchanged: ScreenObservation | None = None

    async def observe(
        self,
        reason: CaptureReason = CaptureReason.STEP_START,
        question: str | None = None,
    ) -> ScreenObservation:
        """Capture and describe one screen state."""
        frame = self._capture_with_retry()
        previous_hash = self._policy.last_hash
        permitted, why = self._policy.permits(frame)
        if not permitted:
            if self.last_unchanged is not None and why == "unchanged":
                self.last_unchanged.changed_from_previous = False
                return self.last_unchanged
            raise VerificationFailed(
                f"capture not permitted: {why}",
                context={"reason": reason.value},
            )
        self._policy.record(frame)
        self._sequence += 1

        changed = previous_hash is not None and frame.content_hash != previous_hash
        observation = ScreenObservation(
            observation_id=f"obs-{self._sequence:04d}",
            captured_at_ms=frame.captured_at_ms,
            content_hash=frame.content_hash,
            width=frame.width,
            height=frame.height,
            description="",
            changed_from_previous=changed,
        )
        if self._store is not None:
            self._store.store(frame, reason)

        if self._vision_model is not None:
            observation.description = await self._describe_with_retry(frame, question)
        self.last_unchanged = observation
        return observation

    def _capture_with_retry(self) -> CapturedFrame:
        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                frame = self._provider.capture()
                if not frame.image_bytes:
                    raise ValueError("empty capture")
                return frame
            except Exception as exc:  # noqa: BLE001 — BP §240 retry with limits
                last_error = exc
        raise VerificationFailed(
            f"screen capture failed after {self._max_retries + 1} attempts: {last_error}",
            context={"attempts": self._max_retries + 1},
        )

    async def _describe_with_retry(self, frame: CapturedFrame, question: str | None) -> str:
        for _ in range(self._max_retries + 1):
            try:
                return await self._vision_model.describe(  # type: ignore[union-attr]
                    frame.image_bytes, question
                )
            except Exception:  # noqa: BLE001 — BP §240: degrade, never fabricate
                continue
        return ""  # capture-only observation; caller may ask the user (BP §240)


__all__ = [
    "CapturePolicy",
    "CaptureReason",
    "CapturedFrame",
    "ScreenObservation",
    "ScreenshotProvider",
    "ScreenStore",
    "VisionEngine",
]
