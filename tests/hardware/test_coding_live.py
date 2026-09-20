"""HARDWARE §9.31/§9.46: a LIVE local model performs a real coding task.

Repository (temp, real files) -> CodingWorker plan -> REAL gemma3:4b via
Ollama proposes -> Action IR -> authority -> executor edits calculator.py ->
a REAL pytest subprocess runs -> goal verifier proves:
    file_contains "return a * b"  AND  captured 'pytest -q' exit 0.

Test-file integrity is protected by owner-instruction conflicts; the owner
proxy here DENIES any test edits (live model must fix the implementation).
If the live result diverges from the demanded behavior, SUCCESS must not be
reported - the biconditional below IS the assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.coding_helpers import BUGGY_CALC, CALC_TEST, make_repo
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger, EventType
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry

pytestmark = pytest.mark.hardware

MODEL = "qwen3:14b"  # smallest VALIDATED local model for coding turns:
#   gemma3:4b was tried first on this exact task and repeatedly claimed
#   completion without running tests (2026-09-15 runs) - measurement drove
#   this routing decision up one size (SPEC §12/§13).
CODER = "qwen3-coder:30b-a3b-q4_K_M"
TEST_COMMAND_DISPLAY = "pytest -q"


def _app(tmp_path: Path) -> NomadicApp:
    log = EventLogger()
    registry = ModelRegistry(log)
    registry.register(
        ModelRecord(
            model_id=MODEL,
            engine="ollama",
            roles=["coding", "worker"],
            capabilities=[
                CapabilityTag.TEXT,
                CapabilityTag.TOOL_USE,
                CapabilityTag.CODING,
                CapabilityTag.REASONING,
                CapabilityTag.TESTING,
            ],
            health=ModelHealth.HEALTHY,
            source="ollama",
            context_window=32768,
        )
    )
    registry.register(
        ModelRecord(
            model_id=CODER,
            engine="ollama",
            roles=["coding"],
            capabilities=[
                CapabilityTag.TEXT,
                CapabilityTag.TOOL_USE,
                CapabilityTag.CODING,
                CapabilityTag.REASONING,
                CapabilityTag.TESTING,
            ],
            health=ModelHealth.HEALTHY,
            source="ollama",
            context_window=32768,
        )
    )
    app = NomadicApp(
        AppConfig.model_validate({"budget": {"max_recoveries": 5}}),
        registry=registry,
        workspace_root=tmp_path / "ws",
        state_dir=tmp_path / "state",
    )
    app.store.grant_full_pc_autonomy(source="hardware_owner")
    return app


async def test_live_model_coding_task_verified_by_goal_verifier(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        health = await app.engines["ollama"].health()
        if MODEL not in health.models:
            pytest.skip(f"{MODEL} not served by local ollama")
        repo = make_repo(tmp_path)
        goal = (
            "Replace calculator.py so its final content is exactly this code:\n"
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
            "Write that file (include both functions). Then run the tests with the "
            'exact action: command "pytest" and args ["-q"] '
            f"(shown as: {TEST_COMMAND_DISPLAY})."
        )
        predicates = [
            {"type": "file_contains", "path": "calculator.py", "text": "return a * b"},
            {"type": "tests_pass", "command": TEST_COMMAND_DISPLAY},
        ]
        asks: list[dict] = []

        def ask(_conflict: dict) -> str:
            asks.append(_conflict)
            return "DENY"  # owner never permits test tampering

        summary = None
        for attempt in range(3):
            summary, report = await app.run_coding(
                goal,
                repo=repo,
                predicates=predicates,
                ask_owner=ask,
                task_id=f"livecode{attempt}",
            )
            if summary.status is TaskStatus.SUCCESS:
                break
        assert summary is not None
        calc = (repo / "calculator.py").read_text(encoding="utf-8")
        test_file = (repo / "test_calculator.py").read_text(encoding="utf-8")
        print(
            "LIVE CODING:",
            summary.status.value,
            summary.goal_verdict,
            "tests_run=",
            report.tests_run if report else None,
            "exits=",
            report.test_exits if report else None,
            "asked=",
            len(asks),
        )
        # --- invariants (must hold no matter what the live model did) -------
        # test file was NEVER tampered with (protected + DENY + executor truth)
        assert test_file == CALC_TEST
        for a in asks:
            assert "test" in str(a.get("resource", "")).lower()

        # biconditional: SUCCESS only with real verified evidence
        disk_ok = "return a * b" in calc and "def add(" in calc
        pytest_ran = any(e == 0 for e in (report.test_exits if report else []))
        if summary.status is TaskStatus.SUCCESS:
            assert summary.goal_verdict == "PASS"
            assert disk_ok and pytest_ran
            assert summary.model_id in {MODEL, CODER}
        else:
            # never a fake success: if it claims failure/partial, either the
            # fix or the (real) test run genuinely did not happen yet
            assert summary.status in {
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.PARTIAL,
            }
        # every side effect went through the executor+audit: tool events exist
        exec_events = [
            e for e in app.log.events(summary.task_id) if e.type is EventType.TOOL_EXECUTED
        ]
        assert exec_events, "expected real executor activity for the live task"
        # The task definition above IS the goal; the outcome is honest either way.
        assert BUGGY_CALC != calc, "expected at least a first implementation attempt"
    finally:
        await app.aclose()


async def test_live_model_repair_path_demonstrated(tmp_path: Path) -> None:
    """HOTFIX live check (asserts invariants, never a required outcome):
    a genuine exit-1 pytest run followed by a genuine exit-0 run means the
    repair path was USED; if the live model instead stalls, status must not
    be SUCCESS. REPAIR_PATH_DEMONSTRATED is reported, not demanded."""
    app = _app(tmp_path)
    try:
        health = await app.engines["ollama"].health()
        if MODEL not in health.models:
            pytest.skip(f"{MODEL} not served")
        repo = tmp_path / "rep2"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "calculator.py").write_text(BUGGY_CALC, encoding="utf-8")
        (repo / "test_calculator.py").write_text(CALC_TEST, encoding="utf-8")
        goal = (
            "Two-phase fix demo you must actually execute in order: "
            'phase 1 - run the tests once via terminal command "pytest" args ["-q"] '
            "(the current code is buggy, a failing run is expected); "
            "phase 2 - repair multiply to return a * b, then run the tests again "
            "so they PASS. Final calculator.py must be the fixed version. "
            "Never modify test files."
        )
        preds = [{"type": "tests_pass", "command": "pytest -q"}]
        summary, report = await app.run_coding(
            goal,
            repo=repo,
            predicates=preds,
            ask_owner=lambda c: "DENY",
            task_id=f"liverep{tmp_path.stem[:6]}",
        )
        exit_codes = list(report.test_exits)
        demonstrated = 1 in exit_codes and 0 in exit_codes[exit_codes.index(1) + 1 :]
        print(
            "REPAIR_PATH_DEMONSTRATED:",
            demonstrated,
            "| status:",
            summary.status.value,
            "| verdict:",
            summary.goal_verdict,
            "| exits:",
            exit_codes,
        )
        # implications only - no required outcome (deterministic tests own
        # the SUCCESS gate; this proves the routing change live)
        assert (repo / "test_calculator.py").read_text(encoding="utf-8") == CALC_TEST
        if demonstrated:
            later_fix = (repo / "calculator.py").read_text(encoding="utf-8")
            assert "return a * b" in later_fix or summary.status is not TaskStatus.SUCCESS
        if summary.status is TaskStatus.SUCCESS:
            assert summary.goal_verdict == "PASS" and 0 in exit_codes
        assert summary.status is not TaskStatus.SUCCESS or 0 in exit_codes
    finally:
        await app.aclose()
