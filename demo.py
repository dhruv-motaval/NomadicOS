"""End-to-end demonstration: BP §78 first milestone on fakes (CI path).

Run:  python demo.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from nomadicos.core.runtime import Runtime  # noqa: E402
from nomadicos.models.base import ModelStatus  # noqa: E402
from nomadicos.models.fake import FakeLocalModel  # noqa: E402


def build_demo_model(workspace: Path) -> FakeLocalModel:
    listing = json.dumps(
        {
            "tool": "filesystem",
            "arguments": {"action": "list", "path": str(workspace)},
            "finished": False,
        }
    )
    finish = json.dumps({"finished": True})
    model = FakeLocalModel("fake/demo-planner")
    model._responses = [
        json.dumps(
            {
                "tool": "filesystem",
                "arguments": {
                    "action": "write",
                    "path": str(workspace / "report.txt"),
                    "content": "NomadicOS v0.1 — all systems nominal.",
                },
                "finished": False,
            }
        ),
        listing,
        finish,
    ]
    model._descriptor.status = ModelStatus.ENABLED
    return model


async def main() -> None:
    workspace = Path(tempfile.mkdtemp()) / "task-workspace"
    workspace.mkdir(parents=True)
    runtime = Runtime()

    # Deterministic demo model (CI-fake; a real GGUF model plugs in via
    # nomadicos.models.llamacpp.LlamaCppModel — ADR-0001).
    runtime.register_model(build_demo_model(workspace))

    goal = "Write the NomadicOS status into report.txt and show the directory listing"
    report = await runtime.run_goal(goal, user_id="local-owner")
    print()
    print("=" * 60)
    print(report.render())
    print("=" * 60)
    print("Experience recorded:", report.experience_id)
    print("Statuses truthfully reflect evidence (BP §366, §180-181).")


if __name__ == "__main__":
    asyncio.run(main())
