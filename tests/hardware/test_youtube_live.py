"""Live task: open Chrome → YouTube → play Zagreera Gaming's latest video.

Deterministic path: public-GET channel discovery + latest-video extraction
(Network Gateway pattern, BP §48), Chrome opened directly on the watch URL —
no blind clicking. BP §145 verification: real screenshot → local gemma3:4b.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
CHANNEL_QUERY = "Zagreera Gaming"


def find_latest() -> tuple[str, str, str]:
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        response = client.get(
            "https://www.youtube.com/results", params={"search_query": CHANNEL_QUERY}
        )
        response.raise_for_status()
        handle_match = re.search(r'"canonicalBaseUrl":"(/@[^"]+)"', response.text)
        if not handle_match:
            raise RuntimeError("channel not found")
        handle = handle_match.group(1)
        print(f"channel: https://www.youtube.com{handle}")

        response = client.get(f"https://www.youtube.com{handle}/videos")
        response.raise_for_status()
        vid = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', response.text)[0]
        index = response.text.find(f'"videoId":"{vid}"')
        window = response.text[index: index + 4000]
        title_match = re.search(r'"title":\{"content":"(.*?)"', window, re.DOTALL)
        title = title_match.group(1) if title_match else "latest upload"
        return handle, vid, title


def verify_on_screen() -> str:
    """BP §145: screenshot → local gemma3:4b confirms YouTube state."""
    time.sleep(7)
    import io

    from PIL import ImageGrab

    from nomadicos.models.ollama_adapter import OllamaModel

    image = ImageGrab.grab()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    gemma = OllamaModel("ollama/gemma3:4b")

    async def describe():
        await gemma.load()
        try:
            return await gemma.describe(
                buffer.getvalue(),
                "Is YouTube open and is a video playing or loaded? Answer briefly: "
                "yes/no and what you see.",
                max_tokens=200,
            )
        finally:
            await gemma.unload()

    import asyncio

    return asyncio.run(describe())


if __name__ == "__main__":
    handle, video_id, title = find_latest()
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"latest video: {title}")
    print(f"opening: {watch_url}")
    subprocess.Popen(["cmd", "/c", "start", "chrome", watch_url])
    print("Chrome launched — verifying on screen via local vision...")
    answer = verify_on_screen()
    safe = answer.strip()[:300].encode("ascii", "replace").decode("ascii")
    print(f"screen verification: {safe}")
