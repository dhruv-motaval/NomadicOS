"""STEP 6A.5 — FINAL COMPLETION-GATE test (qwen3:14b).

Same corrupted-markdown scenario, corruption injected by the existing harness
mechanism (one line prepended, identical to run_repair_fidelity_6a5.py). The
prior failing suite state is OBSERVED AND RECORDED BY THE HARNESS before the
task starts (node exit + SyntaxError captured, no line number passed to the
model) — because under the frozen lifecycle a model step that executes a
non-zero-exit command caps the attempt at PARTIALLY_COMPLETED
(executor.py ACTION_FAILED -> runtime.py:failed -> never SUCCESS within the
same attempt). Everything the model does inside the task must SUCCEED, so the
model may not spend a step re-observing the defect; instead it must do the
mandatory POST-REPAIR verification in-loop:

    filesystem.read -> filesystem.write (production ALLOW+EXECUTED)
    -> terminal 'node test.js' (must actually run AFTER the write and pass)
    -> finished=true -> lifecycle SUCCESS -> PostgreSQL SUCCESS

No runtime/security/verifier/lifecycle change; pinning via existing
run_goal(model_id=...). Harness verification after the task NEVER counts —
POST_REPAIR_VERIFICATION_* are computed ONLY from production audit events
strictly later than the write EXECUTED event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_coding_agent_e2e_6a5 import WS_REL  # noqa: E402
from run_repair_fidelity_6a5 import (  # noqa: E402
    MODEL_ID,
    TRACE,
    _dump_audit,
    _hook_trace,
    _node_tests,
    _sha,
)

from nomadicos.core.runtime import Runtime  # noqa: E402

GATE_GOAL = (
    f"The project in `{WS_REL}` has a broken test suite: `node test.js` in working_dir "
    f"'{WS_REL}' currently exits non-zero with a SyntaxError raised before any test "
    "runs (this failure was already observed and recorded by the run supervisor — do "
    "NOT waste steps reproducing it). Fix it with the SMALLEST POSSIBLE CHANGE: "
    f"(1) filesystem: action 'read' '{WS_REL}/markdown.js' to get the complete current "
    "content, find the single corrupted line yourself; (2) filesystem: action 'write' "
    f"'{WS_REL}/markdown.js' with the SAME complete content but ONLY the corrupted line "
    "removed — every other line must survive byte-for-byte; do not reimplement or "
    f"reformat anything; (3) MANDATORY VERIFICATION: terminal: command 'node', args "
    f"['test.js'], working_dir '{WS_REL}' — this verification run MUST happen AFTER "
    "your repair write and MUST exit 0 with 'All tests passed'. If it fails, read again, "
    "correct only the remaining defect, and verify again. NEVER modify test.js — it is "
    "the contract. You are FORBIDDEN from declaring finished=true until the post-repair "
    "verification step has actually executed and passed."
)


def _fld(e: Any) -> dict:
    f = e.get("fields") or "{}"
    return f if isinstance(f, dict) else json.loads(f)


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
    prefix = b"const broken = ((; // injected\n"
    ok0, d0 = _node_tests(ws)
    cur = target.read_bytes()
    clean_bytes = cur[len(prefix) :] if cur.startswith(prefix) else cur
    clean_sha = hashlib.sha256(clean_bytes).hexdigest()
    if ok0:
        target.write_bytes(prefix + cur)
        corrupt_sha = _sha(target)
        print(
            f"[SETUP] green baseline injected corruption; baseline={clean_sha[:16]} "
            f"corrupted={corrupt_sha[:16]}"
        )
    elif cur.startswith(prefix):
        corrupt_sha = _sha(target)
        print(
            f"[SETUP] scenario already corrupted (defect from aborted run); "
            f"corrupted={corrupt_sha[:16]} baseline={clean_sha[:16]}"
        )
    else:
        print(f"BLOCKED: scenario in unexpected state (not green, defect not line 1): {d0[:90]}")
        return 2
    ok_pre, d_pre = _node_tests(ws)
    err_line = d_pre.replace("\n", " ")[:200]
    print(f"[SETUP] baseline sha={clean_sha[:16]} corrupted sha={corrupt_sha[:16]}")
    print(f"[SETUP] HARNESS-AUTHORED failure observation (pre-task): green_now={ok_pre}")
    print(f"[SETUP] node output (truncated): {err_line[:140]}")

    report = await rt.run_goal(
        GATE_GOAL, model_id=MODEL_ID, max_steps=14, max_duration_seconds=1200
    )
    after_sha = _sha(target)
    changed = after_sha != corrupt_sha
    fidelity = after_sha == clean_sha
    ok_final, d_final = _node_tests(ws)

    from nomadicos.postgres.repositories import TaskRepository

    row = await TaskRepository(rt._pg_client).get(UUID(report.task_id))  # noqa: SLF001
    db_status = row["status"] if row else "?"

    ev = rt._pg_client.execute(  # noqa: SLF001
        "select occurred_at, decision, step_id, subject, fields from audit_events "
        "where task_id=%s order by occurred_at asc",
        (report.task_id,),
    )
    write_execs = [
        e
        for e in ev
        if e["decision"] == "EXECUTED" and _fld(e).get("capability") == "filesystem.write"
    ]
    write_allows = [
        e
        for e in ev
        if e["decision"] == "ALLOW" and _fld(e).get("capability") == "filesystem.write"
    ]
    terminal_events = [e for e in ev if str(_fld(e).get("capability", "")).startswith("terminal")]
    verification_ok = False
    verification_events = []
    if write_execs:
        w_t = write_execs[-1]["occurred_at"]
        post = [
            e
            for e in terminal_events
            if e["occurred_at"] > w_t and e["decision"] in ("ALLOW", "EXECUTED")
        ]
        verification_events = post
        verification_ok = any(e["decision"] == "EXECUTED" for e in post) and not any(
            e["decision"] == "ACTION_FAILED" for e in post
        )
    step_ids = sorted({str(e["step_id"]) for e in ev if e["step_id"]})
    print(
        f"[RUN] task={report.task_id} status={report.status.value} "
        f"steps={len(report.completed)} completed_steps={step_ids}"
    )
    print(
        f"[FILE] corrupt={corrupt_sha[:16]} after={after_sha[:16]} changed={changed} "
        f"byte-identical-to-clean={fidelity}"
    )
    print(f"[HARNESS-RECHECK] node test.js post-experiment: {'PASS' if ok_final else 'FAIL'}")
    print(
        f"[DATABASE] runtime={report.status.value} postgres={db_status} "
        f"match={report.status.value == db_status}"
    )
    print(f"[MODEL-AUTHORED] write ALLOW×{len(write_allows)} EXECUTED×{len(write_execs)}")
    for w in write_allows:
        print(
            f"  write ALLOW    {w['occurred_at']} step={w['step_id']} "
            f"resource={_fld(w).get('resource')}"
        )
    for w in write_execs:
        print(
            f"  write EXECUTED {w['occurred_at']} step={w['step_id']} "
            f"resource={_fld(w).get('resource')}"
        )
    for e in verification_events:
        f = _fld(e)
        print(
            f"  [POST-REPAIR] {e['occurred_at']} {e['decision']:<10} step={e['step_id']} "
            f"{f.get('capability')} {f.get('resource')}"
        )
    models = rt._pg_client.execute(  # noqa: SLF001
        "select distinct subject from audit_events where task_id=%s and subject like 'ollama/%%'",
        (report.task_id,),
    )
    print(f"[MODELS-SEEN] {[m['subject'] for m in models]}")
    claims = 0
    writes_claims = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        claims += 1
        o = json.loads(line)
        if '"filesystem"' in o["raw_proposal"] and '"write"' in o["raw_proposal"]:
            writes_claims.append((o["ts"], o["model_id"]))
    print(f"[TRACE] proposals={claims} filesystem-write-claims={writes_claims}")
    _dump_audit(rt._pg_client, report.task_id)  # noqa: SLF001

    authored = bool(write_allows) and bool(write_execs) and changed
    gates = {
        "MODEL_AUTHORED_REPAIR_WRITE": authored,
        "POST_REPAIR_VERIFICATION_EXECUTED": verification_ok,
        "POST_REPAIR_VERIFICATION_PASSED": verification_ok and ok_final,
        "RUNTIME_STATE": report.status.value,
        "POSTGRES_STATE": db_status,
        "RUNTIME_EQUALS_DB": report.status.value == db_status,
    }
    passed = all(
        [
            authored,
            verification_ok,
            ok_final,
            report.status.value == "SUCCESS",
            db_status == "SUCCESS",
            report.status.value == db_status,
        ]
    )
    print(f"\n[RESULT] {'PASS' if passed else 'HONEST-FAILURE'} | gates={gates}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
