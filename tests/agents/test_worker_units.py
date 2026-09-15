"""Phase 9 worker units: contract, inspector bounds, planner, guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.agents import CodingPlanner, CodingWorker, RepoInspector, Worker, WorkerReport
from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.kernel.config import ModelRoles  # noqa: F401 - DI typing sanity

# ---------------------------------------------------------- worker contract --


def test_coding_worker_satisfies_protocol_and_reports_are_clean() -> None:
    worker: Worker = CodingWorker(Path("."))
    assert worker.name == "coding-worker-v1" and worker.kind == "coding"
    fields = set(worker.report_fields())
    forbidden = {
        "authorized",
        "allowed",
        "approved",
        "permission",
        "capability",
        "owner_approved",
        "bypass",
        "grant",
        "verified",
        "success",
    }
    assert not (fields & {f.lower() for f in forbidden})
    with pytest.raises(ValidationError):
        WorkerReport.model_validate(
            {
                "worker": "coding-worker-v1",
                "task_id": "t",
                "authorized": True,  # smuggled authority must fail
            }
        )


def test_injector_excludes_secrets_and_gits_never_auto_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=hunter2", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("PRIVKEY", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
    survey = RepoInspector(tmp_path).survey(focus_terms=["app.py"])
    assert not any(".env" in p for p in survey.tree)
    assert not any("id_rsa" in name for name, _ in survey.excerpts)
    assert any(name == "app.py" for name, _ in survey.excerpts)


def test_inspector_bounds(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"f{i}.py").write_text("x" * 50, encoding="utf-8")
    (tmp_path / "huge.py").write_text("y" * 90_000, encoding="utf-8")
    insp = RepoInspector(tmp_path)
    survey = insp.survey(focus_terms=["huge.py"])
    assert len(survey.tree) <= 200
    if any(name == "huge.py" for name, _ in survey.excerpts):
        excerpt = dict(survey.excerpts)["huge.py"]
        assert "too large" in excerpt
    section = survey.as_prompt_section()
    assert len(section) <= 12_000
    # never raises on weird chars in names
    (tmp_path / "weïrd §.py").write_text("z", encoding="utf-8")
    insp.survey()


def test_planner_is_deterministic_bounded_and_goal_subordinate() -> None:
    goal = Goal.from_spec(
        "fix multiply",
        predicates=[
            {"type": "file_contains", "path": "calculator.py", "text": "a * b"},
            {"type": "tests_pass", "command": "pytest -q"},
        ],
    )
    planner = CodingPlanner(has_git=False)
    plan = planner.plan(goal, None)
    again = CodingPlanner(has_git=False).plan(goal, None)
    assert [s.description for s in plan.steps] == [s.description for s in again.steps]
    kinds = [s.description.split(" ")[0] for s in plan.steps]
    assert kinds[0] == "Inspect" and any("test" in s.description.lower() for s in plan.steps)
    assert len(plan.steps) <= 12
    # no step may rewrite the goal: the plan holds steps, not predicate edits
    assert plan.task_id == goal.id
    git_plan = CodingPlanner(has_git=True).plan(goal, None)
    assert any("git" in s.description for s in git_plan.steps)


def test_owner_predicates_builder() -> None:
    from nomadicos.agents.planning import predicates_for_coding_task

    preds = predicates_for_coding_task(
        test_command="pytest -q", require_files=["src/x.py"], require_content=[("a", "b")]
    )
    assert preds == [
        {"type": "file_exists", "path": "src/x.py"},
        {"type": "file_contains", "path": "a", "text": "b"},
        {"type": "tests_pass", "command": "pytest -q"},
    ]
    assert predicates_for_coding_task(test_command=None) == []


# ------------------------------------------------------------- AST guards ---


def test_worker_modules_never_touch_execution_or_authority_directly() -> None:
    """§9.3/§9.41: agents import nothing from authority/executor/tools/
    orchestration and run no OS/network mutation primitives."""
    forbidden_modules = (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.tools",
        "nomadicos.orchestration",
    )
    forbidden_calls = {
        "subprocess",
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
    }
    pkg = Path("src/nomadicos/agents")
    for file in sorted(pkg.glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(
                        ("subprocess", "shutil", "socket", "httpx", "urllib")
                    )
                    assert not any(alias.name.startswith(m) for m in forbidden_modules)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not any(mod.startswith(m) for m in forbidden_modules)
                assert "subprocess" not in mod and "shutil" not in mod
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "open":
                    mode = node.args[1] if len(node.args) > 1 else None
                    assert isinstance(mode, ast.Constant) and str(mode.value) == "rb", (
                        f"{file.name}: non-read-only open"
                    )
                else:
                    assert name not in forbidden_calls, f"{file.name}: {name}"


def test_run_summary_taskstatus_import_shape() -> None:
    assert TaskStatus.SUCCESS != TaskStatus.PARTIAL
