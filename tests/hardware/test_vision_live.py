"""Live validation: real screen capture → real gemma vision → observation.

Hardware test (BP §172, §175): runs on the owner's desktop, fully local (I11).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from nomadicos.models.ollama_adapter import OllamaModel
from nomadicos.vision.base import CapturePolicy, CaptureReason, VisionEngine
from nomadicos.vision.parser import parse_observation
from nomadicos.vision.screenshot import PillowScreenshotProvider


async def main() -> None:
    provider = PillowScreenshotProvider()
    engine = VisionEngine(
        provider,
        policy=CapturePolicy(min_interval_ms=0),
        max_retries=1,
    )

    # 1. Capture the real screen (local only, BP §159).
    observation = await engine.observe(CaptureReason.EXPLICIT_REQUEST)
    print(f"capture: {observation.width}x{observation.height} hash={observation.content_hash}")

    # 2. Real gemma vision through the local Ollama server (ADR-0004).
    gemma = OllamaModel("ollama/gemma3:4b")
    await gemma.load()
    description = await gemma.describe(
        provider.capture().image_bytes,
        "Describe this screen in 2 sentences: what application is open and what text is visible?",
        max_tokens=256,
    )
    print(f"gemma description: {description[:300]}")

    # 3. Parse into grounded elements (BP §177).
    observation.description = description
    parsed = parse_observation(observation)
    print(f"parsed elements: {[(e['kind'], e.get('label')) for e in parsed.elements]}")

    await gemma.unload()


if __name__ == "__main__":
    asyncio.run(main())
