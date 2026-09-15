"""STEP 6A.5 PHASE 2 — REPAIR-FIDELITY test (qwen3:14b).

Precondition handling (no external repair): the workspace's markdown.js was
wrecked by the previous experiment's low-fidelity model rewrite. Rather than
restoring the file outside the runtime, stage A re-runs the ORIGINAL shipped
6A.5 ticket flow (imported from run_coding_agent_e2e_6a5.py) through the
production loop, now pinned to qwen3:14b, so the "known-good" baseline
markdown.js is itself model-authored. Stage B injects the identical corruption
(one line prepended) and lets qwen3:14b repair it under a minimal-change
procedure. Fidelity oracle: repaired file must be byte-identical to the clean
baseline recorded BEFORE corruption.

Stage A regenerates markdown.js to satisfy the untouched original test.js
contract; the harness only verifies (subprocess node), never authors project
code. Trace: data/logs/6a5_fidelity.jsonl.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_coding_agent_e2e_6a5 import TICKETS, WS_REL  # noqa: E402

from nomadicos.core.runtime import Runtime  # noqa: E402

TRACE = ROOT / "data" / "logs" / "6a5_fidelity.jsonl"
MODEL_ID = "ollama/qwen3:14b"

REPAIR_GOAL = (
    f"URGENT: the Markdown editor in `{WS_REL}` is BROKEN: `node test.js` fails with a "
    "syntax error before any test runs. Repair it with the SMALLEST POSSIBLE CHANGE, "
    "following this procedure with the tools: (1) terminal: command 'node', args "
    f"['test.js'], working_dir '{WS_REL}' to observe the failure; (2) filesystem: action "
    f"'read' '{WS_REL}/markdown.js' to obtain the COMPLETE current file content, then "
    "determine yourself exactly which single line breaks it; (3) filesystem: action "
    f"'write' '{WS_REL}/markdown.js' with the complete file content EXACTLY as you read "
    "it, except with ONLY the broken line removed — do not rewrite, reimplement, rename, "
    "reformat, or improve any other line; every existing function must survive "
    "byte-for-byte; (4) run 'node test.js' again — it must print ALL TESTS PASSED; if it "
    "still fails, read the file again and fix only whatever is still wrong, never "
    "rewriting working code. NEVER modify test.js — it is the contract. Set finished=true "
    "only after the tests pass."
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _node_tests(ws: Path) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["node", "test.js"], cwd=str(ws), capture_output=True, text=True, timeout=30
        )
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-160:]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _hook_trace(rt: Runtime) -> None:
    """All enabled real Ollama models (attribution-safe, same pattern as
    run_coding_agent_e2e_6a5.py) so no proposal can be mis-assigned."""
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    TRACE.write_text("", encoding="utf-8")
    status = rt.manager.snapshot()["status"]
    if status.get(MODEL_ID) != "enabled":
        raise SystemExit(f"BLOCKED: {MODEL_ID} not enabled — no local model, not faking")
    rt.manager._models[MODEL_ID]._timeout = 600.0  # noqa: SLF001
    for model_id, model in list(rt.manager._models.items()):  # noqa: SLF001
        if model_id.startswith("fake/") or status.get(model_id) != "enabled":
            continue
        if not hasattr(model, "generate"):
            continue
        orig = model.generate

        async def _wrapped(request: Any, _orig=orig, _mid=model_id) -> Any:
            t0 = time.monotonic()
            res = await _orig(request)
            with TRACE.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": round(time.time(), 3),
                            "model_id": _mid,
                            "prompt_chars": len(request.prompt),
                            "raw_proposal": res.text[:4000],
                            "duration_ms": round((time.monotonic() - t0) * 1000),
                        }
                    )
                    + "\n"
                )
            return res

        model.generate = _wrapped  # type: ignore[method-assign]


def _dump_audit(c: Any, task_id: str) -> None:
    rows = c.execute(
        "select occurred_at, decision, step_id, fields from audit_events where task_id=%s "
        "order by occurred_at asc",
        (task_id,),
    )
    for r in rows:
        f = r["fields"] if isinstance(r["fields"], dict) else json.loads(r["fields"] or "{}")
        print(
            f"  {r['occurred_at'].strftime('%H:%M:%S')} {r['decision']:<44} "
            f"step={r['step_id'] or '-':<18} {f.get('capability', '')} {f.get('resource', '')}"
        )


async def _ticket(rt: Runtime, name: str, ws: Path) -> None:
    """Stage A: one shipped 6A.5 ticket, production loop, same 3-attempt
    feedback retry pattern as run_coding_agent_e2e_6a5.py PHASE 1."""
    goal, predicate = next((g, p) for n, g, p in TICKETS if n == name)
    if predicate(ws)[0]:
        print(f"[A:{name}] already satisfied by the in-loop rebuild — skipping")
        return
    attempts = 0
    ok, detail = False, ""
    while attempts < 3:
        attempts += 1
        files = ", ".join(sorted(p.name for p in ws.glob("*") if p.is_file()))
        ticket = (
            goal
            if attempts == 1
            else (
                f"Your previous attempt at this ticket did NOT pass verification ({detail}). "
                f"Existing files: {files}. "
                f"Try again, fixing only the problem: {goal}"
            )
        )
        r = await rt.run_goal(ticket, model_id=MODEL_ID, max_steps=10, max_duration_seconds=900)
        ok, detail = predicate(ws)
        print(
            f"[A:{name}] status={r.status.value} steps={len(r.completed)} "
            f"verified={ok} ({detail[:80]}) try={attempts} task={r.task_id}"
        )
        if ok:
            return
    raise SystemExit(f"BLOCKED: stage A ticket '{name}' not achievable by {MODEL_ID}: {detail}")


async def main() -> int:
    rt = Runtime()
    if not rt._pg_available:  # noqa: SLF001
        print("BLOCKED: PostgreSQL not reachable")
        return 2
    await rt.register_ollama_models()
    rt.permissions.grant(
        "filesystem", granted_by="local-owner (6A.5 pre-authorization)", one_shot=False
    )
    rt.permissions.grant(
        "terminal", granted_by="local-owner (6A.5 pre-authorization)", one_shot=False
    )
    _hook_trace(rt)

    ws = Path(rt.workspace_root) / WS_REL
    target = ws / "markdown.js"

    # ---------------------------------------------------------- STAGE A: baseline
    ok0, d0 = _node_tests(ws)
    print(f"[PRE] test.js green before any action? {ok0} ({d0[:70]})")
    if not ok0:
        print("[A] known-good baseline missing -> rebuild IN-LOOP via original 6A.5 tickets")
        await _ticket(rt, "markdown-js", ws)
        await _ticket(rt, "test-js", ws)
    ok_before, d_before = _node_tests(ws)
    if not ok_before:
        print(f"BLOCKED: stage A did not restore green baseline: {d_before}")
        return 2
    clean_sha = _sha(target)
    clean_bytes = target.read_bytes()
    print(f"[A] clean baseline sha256={clean_sha[:16]}")

    # ------------------------------------------------- STAGE B: corrupt & repair
    target.write_bytes(b"const broken = ((; // injected\n" + clean_bytes)
    sha_corrupt = _sha(target)
    ok_red, d_red = _node_tests(ws)
    print(f"[B] corruption injected sha={sha_corrupt[:16]} tests_green={ok_red} ({d_red[:60]})")

    t0 = time.monotonic()
    report = await rt.run_goal(
        REPAIR_GOAL, model_id=MODEL_ID, max_steps=14, max_duration_seconds=1200
    )
    dur = time.monotonic() - t0
    sha_after = _sha(target)
    changed = sha_after != sha_corrupt
    fidelity = sha_after == clean_sha
    print(
        f"[B] task={report.task_id} status={report.status.value} "
        f"steps={len(report.completed)} ({dur:.0f}s)"
    )
    print(
        f"[FILE] corrupt={sha_corrupt[:16]} after={sha_after[:16]} changed={changed} "
        f"byte-identical-to-clean={fidelity}"
    )
    ok_final, d_final = _node_tests(ws)
    print(f"[VERIFY] tests={'PASS' if ok_final else 'FAIL'} ({d_final[:90]})")

    from nomadicos.postgres.repositories import TaskRepository

    row = await TaskRepository(rt._pg_client).get(UUID(report.task_id))  # noqa: SLF001
    db_status = row["status"] if row else "?"
    print(
        f"[PG] runtime={report.status.value} postgres={db_status} "
        f"match={report.status.value == db_status}"
    )
    write_events = rt._pg_client.execute(  # noqa: SLF001
        "select occurred_at, decision, step_id, fields from audit_events where task_id=%s "
        "and fields->>'capability' = 'filesystem.write' order by occurred_at asc",
        (report.task_id,),
    )
    for e in write_events:
        f = e["fields"] if isinstance(e["fields"], dict) else json.loads(e["fields"] or "{}")
        print(
            f"  [WRITE] {e['occurred_at']} {e['decision']} step={e['step_id']} "
            f"resource={f.get('resource')} size={f.get('argument_count')}"
        )
    _dump_audit(rt._pg_client, report.task_id)  # noqa: SLF001
    models = rt._pg_client.execute(  # noqa: SLF001
        "select distinct subject from audit_events where task_id=%s and subject like 'ollama/%%'",
        (report.task_id,),
    )
    print(f"[MODELS-SEEN] {[m['subject'] for m in models]}")

    claims: list[tuple[float, str]] = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        o = json.loads(line)
        if '"filesystem"' in o["raw_proposal"] and '"write"' in o["raw_proposal"]:
            claims.append((o["ts"], o["model_id"], o["raw_proposal"][:260].replace("\n", " ")))
    print(f"[WRITE-PROPOSALS] {len(claims)}")
    primary_wrote_any = any(m == MODEL_ID for _, m, _ in claims)
    # attribute only the proposal that precedes the successful final write
    valid = (not changed) or len(write_events) >= 2
    passed = (
        valid
        and changed
        and fidelity
        and ok_final
        and report.status.value == "SUCCESS"
        and db_status == report.status.value == "SUCCESS"
        and primary_wrote_any
    )
    authored = changed and ok_final and len(write_events) >= 2 and valid
    print(
        f"\n[RESULT] {'PASS' if passed else 'HONEST-FAILURE'}"
        f" | MODEL_AUTHORED_REPAIR_WRITE={'YES' if authored else 'NO'}"
        f" | REPAIR_FIDELITY={'PASS' if fidelity else 'FAIL'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
