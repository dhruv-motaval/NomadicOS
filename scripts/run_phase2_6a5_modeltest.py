"""STEP 6A.5 PHASE 2 — final real model-in-loop repair validation (qwen3:14b).

Scenario identical to the 6A.5 Phase 2 experiment (run_coding_agent_e2e_6a5.py
PHASE 2): the same corrupted-markdown task, same workspace, same tools, same
policy/authorization, same IR -> gateway -> gate -> executor -> verifier ->
lifecycle -> PostgreSQL loop, same normalized stuck guard, same limits.
The ONLY variables: the in-loop model (qwen3:14b instead of qwen3-coder:30b)
and a goal that states the symptom WITHOUT naming the corrupted line, so
diagnosis must be the model's own.

This harness NEVER touches markdown.js after recording the starting state:
it contains no edit/write path. External-edit protection is asserted at the
end: a content change without a production filesystem.write EXECUTED event
=> INVALID.
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

from nomadicos.core.runtime import Runtime  # noqa: E402

WS_REL = "mdstudio-6a5"
MODEL_ID = "ollama/qwen3:14b"
TRACE = ROOT / "data" / "logs" / "6a5_p2_qwen314b.jsonl"

GOAL = (
    f"The Markdown editor project in `{WS_REL}` is BROKEN: its test suite fails. "
    f"Diagnose and repair it using the tools: (1) terminal: command 'node', args "
    f"['test.js'], working_dir '{WS_REL}' to observe the failure; (2) filesystem: "
    f"read the source files involved and determine the root cause yourself; (3) "
    f"filesystem: write the repaired source file back with the fix; (4) run "
    f"'node test.js' again until it prints ALL TESTS PASSED; (5) run 'node build.js' "
    f"(same working_dir) and list '{WS_REL}/dist' to confirm bundle.js exists. "
    f"NEVER edit test.js — the tests are the contract. Set finished=true only "
    f"after the tests pass AND dist/bundle.js exists."
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _node_tests(ws: Path) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["node", "test.js"], cwd=str(ws), capture_output=True, text=True, timeout=30
        )
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-160:]
    except Exception as exc:  # noqa: BLE001 — verifier reports, never crashes
        return False, f"{type(exc).__name__}: {exc}"


def _hook_trace(rt: Runtime) -> None:
    """Append raw model proposals for EVERY enabled real Ollama model (same
    pattern as run_coding_agent_e2e_6a5.py:_hook_proposal_logging) so that an
    attempt-2 escalation model's proposals are attributed to that model, never
    silently lumped under the pinned primary id."""
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    TRACE.write_text("", encoding="utf-8")
    if MODEL_ID not in rt.manager._models:  # noqa: SLF001 — driver instrumentation
        raise SystemExit(f"BLOCKED: {MODEL_ID} not registered — no local model, not faking")
    primary = rt.manager._models[MODEL_ID]
    primary._timeout = 600.0  # noqa: SLF001 — cold load + generation headroom
    for model_id, model in list(rt.manager._models.items()):  # noqa: SLF001
        status = rt.manager.snapshot()["status"].get(model_id)
        if model_id.startswith("fake/") or status != "enabled" or not hasattr(model, "generate"):
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


def _audit_dump(c: Any, task_id: str) -> None:
    rows = c.execute(
        "select occurred_at, decision, category, subject, step_id, fields from audit_events "
        "where task_id=%s order by occurred_at asc",
        (task_id,),
    )
    for r in rows:
        f = r["fields"] if isinstance(r["fields"], dict) else json.loads(r["fields"] or "{}")
        cap = f.get("capability", "")
        res = f.get("resource", "")
        print(
            f"  {r['occurred_at'].strftime('%H:%M:%S')} {r['decision']:<42} "
            f"{r['category']:<15} step={str(r['step_id'])[:8]:8} {cap} {res}"
        )


async def main() -> int:
    rt = Runtime()
    if not rt._pg_available:  # noqa: SLF001
        print("BLOCKED: PostgreSQL not reachable — lifecycle authority required")
        return 2
    n = await rt.register_ollama_models()
    print(f"[FLEET] registered={n} target={MODEL_ID}")

    # Same owner pre-authorization as the original 6A.5 harness; policy still
    # decides every action (the grant only pre-answers a prior ASK).
    rt.permissions.grant(
        "filesystem", granted_by="local-owner (6A.5 pre-authorization)", one_shot=False
    )
    rt.permissions.grant(
        "terminal", granted_by="local-owner (6A.5 pre-authorization)", one_shot=False
    )

    ws = Path(rt.workspace_root) / WS_REL
    target = ws / "markdown.js"
    if not (target.exists() and (ws / "test.js").exists() and (ws / "build.js").exists()):
        print("BLOCKED: mdstudio-6a5 scenario files missing — reproduce via the 6A.5 E2E first")
        return 2
    ok, detail = _node_tests(ws)
    if not ok:
        print(f"INVALID PRE-FIX STATE: healthy baseline must pass, test.js fails: {detail}")
        return 2

    # Inject the SAME corruption as run_coding_agent_e2e_6a5.py PHASE 2.
    original = target.read_bytes()
    target.write_bytes(b"const broken = ((; // injected\n" + original)
    sha_before = _sha(target)
    print(
        f"[SETUP] corruption injected; sha256(corrupted)={sha_before[:16]} "
        f"(size={len(original)} bytes clean)"
    )

    _hook_trace(rt)
    t0 = time.monotonic()
    report = await rt.run_goal(GOAL, model_id=MODEL_ID, max_steps=14, max_duration_seconds=1200)
    dur = time.monotonic() - t0
    sha_after = _sha(target)
    changed = sha_after != sha_before
    print(
        f"[RUN] task={report.task_id} status={report.status.value} "
        f"steps={len(report.completed)} ({dur:.0f}s)"
    )
    print(f"[FILE] sha256 after={sha_after[:16]} changed={changed}")

    ok_md, d_md = _node_tests(ws)
    dist = ws / "dist" / "bundle.js"
    print(f"[VERIFY] tests={'PASS' if ok_md else 'FAIL'} ({d_md[:90]}) dist_exists={dist.exists()}")

    # --- production-path evidence + PG parity -------------------------------
    from nomadicos.postgres.repositories import TaskRepository

    repo = TaskRepository(rt._pg_client)  # noqa: SLF001
    row = await repo.get(UUID(report.task_id))
    db_status = row["status"] if row else "?"
    print(
        f"[PG] runtime={report.status.value} postgres={db_status} "
        f"match={report.status.value == db_status}"
    )
    write_events = rt._pg_client.execute(  # noqa: SLF001
        "select occurred_at, decision, step_id, fields from audit_events where task_id=%s "
        "and fields->>%s like %s order by occurred_at asc",
        (report.task_id, "capability", "filesystem.write%"),
    )
    print(f"[WRITE-EVENTS] filesystem.write gate/executed events: {len(write_events)}")
    for e in write_events:
        f = e["fields"] if isinstance(e["fields"], dict) else json.loads(e["fields"] or "{}")
        print(
            f"  {e['occurred_at']} {e['decision']} step={e['step_id']} "
            f"resource={f.get('resource')} args={f.get('argument_count')}"
        )
    print("[AUDIT TRACE]")
    _audit_dump(rt._pg_client, report.task_id)  # noqa: SLF001
    models = rt._pg_client.execute(  # noqa: SLF001
        "select distinct subject from audit_events where task_id=%s and subject like 'ollama/%%'",
        (report.task_id,),
    )
    print(f"[MODELS-SEEN-IN-AUDIT] {[m['subject'] for m in models]}")
    write_claims = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        o = json.loads(line)
        if '"filesystem"' in o["raw_proposal"] and '"write"' in o["raw_proposal"]:
            write_claims.append(
                (o["ts"], o["model_id"], o["raw_proposal"][:300].replace("\n", " "))
            )
    print(f"[WRITE-PROPOSALS] {len(write_claims)} (attributed by generating model):")
    for w in write_claims:
        print(f"  ts={w[0]} model={w[1]} :: {w[2]}")
    outside = Path(rt.workspace_root).parent / "outside-secret.txt"
    outside.unlink(missing_ok=True)

    valid = (not changed) or len(write_events) >= 2  # ALLOW + EXECUTED must back any change
    primary_wrote = any(w[1] == MODEL_ID for w in write_claims)
    result = (
        not valid
        and "INVALID (unexplained file change)"
        or (
            "PASS"
            if (
                changed
                and ok_md
                and report.status.value == "SUCCESS"
                and db_status == "SUCCESS"
                and report.status.value == db_status
                and len(write_events) >= 2
                and primary_wrote
            )
            else "HONEST-FAILURE"
        )
    )
    print(f"[DIST] bundle_exists={dist.exists()}")
    print(
        f"\n[RESULT] {result} | MODEL_AUTHORED_REPAIR_WRITE="
        f"{'YES' if changed and len(write_events) >= 2 and ok_md and primary_wrote else 'NO'} | "
        f"proposals={sum(1 for _ in TRACE.open(encoding='utf-8'))}"
    )
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
