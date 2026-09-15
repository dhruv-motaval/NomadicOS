"""Structural guards for the completion-integrity guarantees (§8.27/§8.32-8.37)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

import nomadicos.orchestration.graph as graph_module
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.orchestration.boundaries import NoGoalVerifier
from nomadicos.verification.goal import PredicateGoalVerifier

# ------------------------------------------ §8.27 single SUCCESS authority --


def test_graph_has_exactly_one_success_assigning_site() -> None:
    src = inspect.getsource(graph_module)
    assert src.count('"task_status": TaskStatus.SUCCESS') == 1
    verify_goal_body = src.split("async def verify_goal")[1].split("async def advance")[0]
    assert '"task_status": TaskStatus.SUCCESS' in verify_goal_body


def test_placeholder_boundary_cannot_produce_pass() -> None:
    r = NoGoalVerifier().verify(Goal(objective="anything"))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED
    assert not r.passed
    bad = NoGoalVerifier().verify(
        Goal.from_spec("x", predicates=[{"type": "file_exists", "path": "/"}])
    )
    assert bad.verdict is VerificationOutcome.NOT_VERIFIED  # even "obviously true"


# --------------------------------------------- §8.33/§8.36 read-only + DI --


def test_verification_modules_are_read_only_and_isolated() -> None:
    """AST-level: no writes, no execution primitives, no authority/tool/
    orchestration imports from inside the verification brick (SPEC §8.33)."""
    import ast

    pkg = Path("src/nomadicos/verification")
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "write_text",
        "write_bytes",
        "mkdir",
        "makedirs",
        "unlink",
        "remove",
        "rename",
        "replace",
        "copy",
    }
    forbidden_modules = (
        "subprocess",
        "shutil",
        "httpx",
        "socket",
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.tools",
        "nomadicos.inference",
        "nomadicos.router",
        "nomadicos.registry",
        "nomadicos.orchestration",
    )
    for file in sorted(pkg.glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(alias.name.startswith(m) for m in forbidden_modules), (
                        f"{file.name}: imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                src_name = node.module or ""
                assert not any(src_name.startswith(m) for m in forbidden_modules), (
                    f"{file.name}: imports from {src_name}"
                )
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "open":  # permitted only for read-only binary reads
                    mode = node.args[1] if len(node.args) > 1 else None
                    assert isinstance(mode, ast.Constant) and str(mode.value) in {"rb", "r"}, (
                        f"{file.name}: open with write mode"
                    )
                else:
                    assert name not in forbidden_calls, f"{file.name}: calls {name}"


# ------------------------------------------------------- §8.14 goal frozen --


def test_goal_object_is_immutable() -> None:
    goal = Goal.from_spec("objective", predicates=[{"type": "file_exists", "path": "a"}])
    with pytest.raises(ValidationError):
        goal.objective = "silently rewritten goal"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        goal.predicates = []  # type: ignore[misc]


def test_recovery_never_rewrites_the_goal(tmp_path: Path) -> None:
    from orchestration.helpers import make_app, write_json

    async def _run() -> None:
        app = make_app(tmp_path, scripts=[("Satisfy", write_json("bad.txt", "WRONG"))])
        await app.run_goal(
            "Create bad.txt",
            predicates=[{"type": "file_content_equals", "path": "bad.txt", "content": "RIGHT"}],
            task_id="goalintact",
        )
        snap = app.graph.get_state({"configurable": {"thread_id": "nomadic:goalintact"}})
        goal = snap.values["goal"]
        assert goal.objective == "Create bad.txt"
        assert goal.id == "goalintact"
        assert len(goal.predicates) == 1
        # recovery really happened (goal stayed authoritative throughout)
        assert len(snap.values.get("recovery_count") or []) >= 1

    asyncio.run(_run())


# ------------------------------------------- §8.2/§8.37 contract coherence --


def test_pass_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(
            level=VerificationLevel.GOAL,
            task_id="t",
            outcome=VerificationOutcome.PASS,
            evidence=[],
        )
    with pytest.raises(ValidationError):
        VerificationResult(
            level=VerificationLevel.GOAL,
            task_id="t",
            outcome=VerificationOutcome.PASS,
            evidence=[EvidenceItem(claim="empty", observed=False)],
        )


def test_outcome_and_evidence_must_cohere() -> None:
    good = VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="t",
        outcome=VerificationOutcome.PASS,
        evidence=[EvidenceItem(claim="x", observed=True)],
        verifier="test-verifier-v1",
    )
    assert good.passed and good.verdict is VerificationOutcome.PASS
    for bad_outcome in (
        VerificationOutcome.NOT_PASS,
        VerificationOutcome.BLOCKED,
        VerificationOutcome.NOT_VERIFIED,
    ):
        with pytest.raises(ValidationError):
            VerificationResult(
                level=VerificationLevel.STEP,
                task_id="t",
                outcome=bad_outcome,
                evidence=[EvidenceItem(claim="y", observed=True)],
            )


def test_four_verdicts_are_distinct() -> None:
    assert len({*VerificationOutcome}) == 4


# ------------------------------------------------------------- aggregator --


def test_goal_aggregator_verdicts(tmp_path: Path) -> None:
    v = PredicateGoalVerifier(workspace_root=tmp_path)
    # no predicates => NOT_VERIFIED (vacuous truth forbidden)
    assert v.verify(Goal(objective="do something impressive")).verdict is (
        VerificationOutcome.NOT_VERIFIED
    )
    # satisfied predicate => PASS with attributable evidence
    goal = Goal(
        objective="file!", predicates=[parse_predicate({"type": "file_exists", "path": "v.txt"})]
    )
    workdir = tmp_path / goal.id
    workdir.mkdir(parents=True)
    (workdir / "v.txt").write_text("x", encoding="utf-8")
    r = v.verify(goal)
    assert r.verdict is VerificationOutcome.PASS
    assert r.verifier == "nomadic-goal-verifier-v1"
    assert r.evidence and any("v.txt" in line for line in r.why())
    # blocked: oversized file cannot be evaluated safely
    big = Goal(
        objective="big",
        predicates=[
            parse_predicate({"type": "file_content_equals", "path": "big.bin", "content": "x"})
        ],
    )
    (tmp_path / big.id).mkdir()
    (tmp_path / big.id / "big.bin").write_bytes(b"0" * 1_500_000)
    assert v.verify(big).verdict is VerificationOutcome.BLOCKED
