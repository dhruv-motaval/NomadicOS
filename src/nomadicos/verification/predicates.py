"""Completion predicates (SPEC §29, §8.4-§8.5, §8.13).

A predicate is DATA describing a testable condition. Evaluation inspects
real evidence (filesystem, captured ExecutionResults) - it can never be
talked into passing by model prose. Unknown predicate types and malformed
expectations are NOT_VERIFIED, never satisfied (fail closed, SPEC §56.14).

No executable predicates exist here by design: no eval(), no shell, no
model-supplied code (SPEC §8.4/§8.34).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nomadicos.contracts.core import GoalPredicate, LogicalOp, Predicate
from nomadicos.contracts.verification import EvidenceItem, VerificationOutcome
from nomadicos.verification.evidence import EvidenceContext

#: maximum recursion of all/any/not compositions (SPEC §8.13)
MAX_DEPTH = 4

#: verification-authority words a predicate must never carry (SPEC §8.5)
PREDICATE_AUTHORITY_FIELDS = frozenset(
    {
        "verified",
        "complete",
        "completed",
        "passed",
        "success",
        "approved",
        "accepted",
        "owner_approved",
        "authorization",
        "capability",
        "bypass",
    }
)


@dataclass
class LeafResult:
    verdict: VerificationOutcome
    items: list[EvidenceItem] = field(default_factory=list)


def _unverifiable(reason: str) -> LeafResult:
    return LeafResult(
        VerificationOutcome.NOT_VERIFIED,
        [EvidenceItem(claim=reason, observed=False, unverifiable=True)],
    )


def _check_authority(p: Predicate) -> LeafResult | None:
    for key in p.fields:
        if str(key).lower() in PREDICATE_AUTHORITY_FIELDS:
            return _unverifiable(
                f"predicate {p.type!r} carries authority field {key!r} - "
                "rejected as data (SPEC §8.5)"
            )
    return None


def _req(p: Predicate, *names: str) -> Any:
    for name in names:
        if isinstance(p.field(name), str) and p.field(name):
            return p.field(name)
        if isinstance(p.field(name), (int, bool)):
            return p.field(name)
    return None


# ---------------------------------------------------------------- leaves --


def _file_exists(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "file")
    if not path:
        return _unverifiable("file_exists: missing 'path'")
    ok = ctx.exists(path)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"file exists: {path}", observed=ok, detail={"path": str(ctx.resolve(path))}
            )
        ],
    )


def _dir_exists(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "directory")
    if not path:
        return _unverifiable("directory_exists: missing 'path'")
    ok = ctx.exists(path, directory=True)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [EvidenceItem(claim=f"directory exists: {path}", observed=ok)],
    )


def _content(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "file")
    expected = _req(p, "content", "text", "content_equals")
    if not path or expected is None:
        return _unverifiable("file_content_equals: missing 'path' or 'content'")
    data, error = ctx.file_bytes(path)
    if error:
        return LeafResult(
            VerificationOutcome.BLOCKED,
            [
                EvidenceItem(
                    claim=f"file unreadable: {path}",
                    observed=False,
                    unverifiable=True,
                    detail={"error": error},
                )
            ],
        )
    if data is None:
        return LeafResult(
            VerificationOutcome.NOT_PASS,
            [
                EvidenceItem(
                    claim=f"content match: {path}",
                    observed=False,
                    detail={"actual": "<absent>", "expected": str(expected)[:80]},
                )
            ],
        )
    actual = data.decode("utf-8", errors="replace")
    ok = actual == expected
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"content match: {path}",
                observed=ok,
                detail={"expected": str(expected)[:80], "actual": actual[:80]},
            )
        ],
    )


def _contains(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "file")
    needle = _req(p, "text", "contains", "content")
    if not path or not needle:
        return _unverifiable("file_contains: missing 'path' or 'text'")
    data, error = ctx.file_bytes(path)
    if error:
        return _unverifiable(f"file_contains: {error}")
    if data is None:
        return LeafResult(
            VerificationOutcome.NOT_PASS,
            [
                EvidenceItem(
                    claim=f"file contains {str(needle)[:60]!r}: {path}",
                    observed=False,
                    detail={"actual": "<absent>"},
                )
            ],
        )
    text = data.decode("utf-8", errors="replace")
    ok = str(needle) in text
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [EvidenceItem(claim=f"file contains {str(needle)[:60]!r}: {path}", observed=ok)],
    )


def _sha(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "file")
    expected = _req(p, "sha256", "hash")
    if not path or not expected:
        return _unverifiable("file_sha256: missing 'path' or 'sha256'")
    actual, error = ctx.file_sha256(path)
    if error:
        return _unverifiable(f"file_sha256: {error}")
    ok = actual == str(expected).lower()
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"sha256 match: {path}",
                observed=ok,
                detail={"expected": str(expected)[:24], "actual": (actual or "<absent>")[:24]},
            )
        ],
    )


def _exec_filter(p: Predicate) -> tuple[str | None, str | None]:
    step = _req(p, "step_id", "step")
    command = _req(p, "command")
    return (str(command) if command else None, str(step) if step else None)


def _exit_code(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    want_raw = p.field("code")
    if want_raw is None:
        want_raw = p.field("exit_code")
    if want_raw is None or not str(want_raw).lstrip("-").isdigit():
        return _unverifiable("exit_code_equals: missing 'code'")
    want = int(want_raw)
    command, step = _exec_filter(p)
    matches = ctx.find_execution(command=command, step_id=step)
    if not matches:
        return _unverifiable("exit_code_equals: no task-correlated matching execution evidence")
    codes = [e.exit_code for e in matches]
    ok = want in codes
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"exit code {want} observed",
                observed=ok,
                detail={"seen": codes[:8], "task": ctx.task_id},
            )
        ],
    )


def _stdout_contains(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    needle = _req(p, "text", "contains", "stdout")
    if not needle:
        return _unverifiable("stdout_contains: missing 'text'")
    command, step = _exec_filter(p)
    matches = ctx.find_execution(command=command, step_id=step)
    if not matches:
        return _unverifiable("stdout_contains: no task-correlated execution evidence")
    ok = any(str(needle) in e.stdout for e in matches)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [EvidenceItem(claim=f"stdout contains {str(needle)[:60]!r}", observed=ok)],
    )


def _stdout_equals(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    expected = _req(p, "text", "stdout_equals")
    if not expected:
        return _unverifiable("stdout_equals: missing 'text'")
    command, step = _exec_filter(p)
    matches = ctx.find_execution(command=command, step_id=step)
    if not matches:
        return _unverifiable("stdout_equals: no task-correlated execution evidence")
    ok = any(e.stdout.strip() == str(expected).strip() for e in matches)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [EvidenceItem(claim=f"stdout equals {str(expected)[:60]!r}", observed=ok)],
    )


def _stderr_contains(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    needle = _req(p, "text", "contains", "stderr")
    if not needle:
        return _unverifiable("stderr_contains: missing 'text'")
    command, step = _exec_filter(p)
    matches = ctx.find_execution(command=command, step_id=step)
    if not matches:
        return _unverifiable("stderr_contains: no task-correlated execution evidence")
    ok = any(str(needle) in e.stderr for e in matches)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [EvidenceItem(claim=f"stderr contains {str(needle)[:60]!r}", observed=ok)],
    )


def _artifact_exists(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    path = _req(p, "path", "artifact")
    if not path:
        return _unverifiable("artifact_exists: missing 'path'")
    claimed = any(
        e.succeeded and str(e.evidence.get("path", "")).endswith(str(path))
        for e in ctx.task_executions()
    )
    live = ctx.exists(path)
    # proof-of-ACTION + proof-of-REALITY: both must hold (SPEC §8.19/§8.20)
    ok = claimed and live
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"artifact produced by THIS task and present: {path}",
                observed=ok,
                detail={"produced_by_task": claimed, "present_now": live},
            )
        ],
    )


def _tests_pass(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    command = _req(p, "command")
    if not command:
        return _unverifiable("tests_pass: missing 'command'")
    matches = ctx.find_execution(command=str(command))
    if not matches:
        return _unverifiable(
            f"tests_pass: command {str(command)[:60]!r} has no captured execution evidence"
        )
    ok = any(e.status.value == "SUCCEEDED" and e.exit_code == 0 for e in matches)
    return LeafResult(
        VerificationOutcome.PASS if ok else VerificationOutcome.NOT_PASS,
        [
            EvidenceItem(
                claim=f"tests passed: {str(command)[:60]}",
                observed=ok,
                detail={"exit_codes": [e.exit_code for e in matches][:5]},
            )
        ],
    )


LEAVES: dict[str, Callable[[Predicate, EvidenceContext], LeafResult]] = {
    "file_exists": _file_exists,
    "directory_exists": _dir_exists,
    "file_content_equals": _content,
    "file_contains": _contains,
    "file_sha256": _sha,
    "exit_code_equals": _exit_code,
    "stdout_contains": _stdout_contains,
    "stdout_equals": _stdout_equals,
    "stderr_contains": _stderr_contains,
    "artifact_exists": _artifact_exists,
    "tests_pass": _tests_pass,
    "process_started": lambda p, ctx: _unverifiable(
        "process_started: process introspection belongs to Phase 12"
    ),
    "api_response_valid": lambda p, ctx: _unverifiable(
        "api_response_valid: unsupported predicate (no evidence contract)"
    ),
    "window_present": lambda p, ctx: _unverifiable(
        "window_present: desktop verification belongs to Phase 12"
    ),
}

SUPPORTED_TYPES = frozenset(LEAVES)


def evaluate_predicate(p: Predicate, ctx: EvidenceContext) -> LeafResult:
    guard = _check_authority(p)
    if guard:
        return guard
    fn = LEAVES.get(p.type)
    if fn is None:
        return _unverifiable(f"unknown predicate type {p.type!r}: never satisfied (SPEC §29)")
    return fn(p, ctx)


def _combine(op: LogicalOp, children: list[LeafResult]) -> LeafResult:
    items = [i for c in children for i in c.items]
    verdicts = [c.verdict for c in children]
    if op is LogicalOp.NOT:
        (child,) = verdicts
        outcome = {
            VerificationOutcome.NOT_PASS: VerificationOutcome.PASS,
            VerificationOutcome.PASS: VerificationOutcome.NOT_PASS,
        }.get(child, child)
        return LeafResult(outcome, items)
    if op is LogicalOp.ANY:
        if VerificationOutcome.PASS in verdicts:
            return LeafResult(VerificationOutcome.PASS, items)
        if VerificationOutcome.BLOCKED in verdicts and all(
            v is VerificationOutcome.BLOCKED for v in verdicts
        ):
            return LeafResult(VerificationOutcome.BLOCKED, items)
        if VerificationOutcome.NOT_PASS in verdicts:
            return LeafResult(VerificationOutcome.NOT_PASS, items)
        return LeafResult(VerificationOutcome.NOT_VERIFIED, items)
    # ALL
    if VerificationOutcome.BLOCKED in verdicts:
        return LeafResult(VerificationOutcome.BLOCKED, items)
    if VerificationOutcome.NOT_PASS in verdicts:
        return LeafResult(VerificationOutcome.NOT_PASS, items)
    if VerificationOutcome.NOT_VERIFIED in verdicts:
        return LeafResult(VerificationOutcome.NOT_VERIFIED, items)
    return LeafResult(VerificationOutcome.PASS, items)


def evaluate_goal_predicate(gp: GoalPredicate, ctx: EvidenceContext, depth: int = 0) -> LeafResult:
    if depth >= MAX_DEPTH:
        return _unverifiable(f"predicate nesting exceeds bound MAX_DEPTH={MAX_DEPTH}")
    if not ctx.charge():
        return _unverifiable("predicate evaluation budget exhausted")
    if gp.predicate is not None:
        return evaluate_predicate(gp.predicate, ctx)
    group = gp.group
    assert group is not None
    children = [evaluate_goal_predicate(child, ctx, depth + 1) for child in group.children]
    return _combine(group.op, children)
