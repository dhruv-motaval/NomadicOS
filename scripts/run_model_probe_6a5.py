"""STEP 6A.5 — minimal real-model one-action probe (harness).

One goal, one real model proposal, full path:
model -> ActionClaim -> IR -> capability -> policy -> AuthorizedAction ->
TaskExecutor -> filesystem -> ToolResult -> verifier -> lifecycle -> PostgreSQL

The ASSERTED path is DERIVED from the actual task workspace (the 6A.5 probe
bug was a hardcoded assumption about where the model would put the file —
the location is the model's choice within the workspace; the name+content
are the contract). Exit 0 = path proven.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nomadicos.core.runtime import Runtime  # noqa: E402  isort: skip

GOAL = (
    "Create a file named model_probe.txt containing exactly HELLO (exactly the "
    "five characters, no newline). Put it anywhere in the task workspace. "
    "Do it with one filesystem write, then set finished=true."
)


async def main() -> int:
    rt = Runtime()
    await rt.register_ollama_models()
    coder = next((m for k, m in rt.manager._models.items() if "qwen3-coder" in k), None)  # noqa: SLF001
    if coder is None:
        print("BLOCKED: qwen3-coder not registered — no real model, not faking")
        return 2
    coder._timeout = 900.0  # noqa: SLF001
    # no permission grants for any tool: the run must succeed under plain policy
    # (owner pre-authorization would weaken the point of the probe)
    workspace = Path(rt.workspace_root).resolve()
    for old in workspace.rglob("model_probe.txt"):  # clean prior probes
        old.unlink()

    t0 = time.monotonic()
    rep = await rt.run_goal(GOAL, max_steps=4, max_duration_seconds=300)
    duration = time.monotonic() - t0

    found = sorted(workspace.rglob("model_probe.txt"))
    row = rt._pg_client.execute("select status from tasks where task_id=%s", (rep.task_id,))
    print(f"[PROBE] task_id={rep.task_id} status={rep.status.value} steps={len(rep.completed)} "
          f"({duration:.0f}s)")
    print(f"[PROBE] verification={rep.verification} failed={rep.failed[:2]}")
    print(f"[PROBE] workspace={workspace}")
    for p in found:
        print(f"[PROBE] candidate={p.relative_to(workspace)} bytes={p.stat().st_size}")
    print(f"[PROBE] postgres={row}")

    ok = (
        rep.status.value == "SUCCESS"
        and len(found) == 1
        and found[0].read_bytes() == b"HELLO"
        and bool(row)
        and row[0]["status"] == rep.status.value
        and any("2/2" in v for v in rep.verification)
    )
    print(f"[PROBE] RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
