"""NomadicOS command-line entry point (SPEC §40). Thin by contract."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="NomadicOS — local-first AI OS.")


def _config():
    from nomadicos.kernel.config import load_config

    return load_config()


async def _engines(cfg):
    from nomadicos.inference.llama_cpp import LlamaCppEngine
    from nomadicos.inference.ollama import OllamaEngine

    engines = {
        "llamacpp": LlamaCppEngine(
            base_url=cfg.inference.llama_server_url,
            default_timeout_s=cfg.inference.request_timeout_s,
        ),
        "ollama": OllamaEngine(
            base_url=cfg.inference.ollama_base_url,
            default_timeout_s=cfg.inference.request_timeout_s,
        ),
    }
    return engines


@app.command()
def health() -> None:
    """Engine + model store health."""
    cfg = _config()

    async def run() -> None:
        engines = await _engines(cfg)
        try:
            for name, engine in engines.items():
                role = "primary" if name == cfg.inference.default_engine else "secondary"
                state = await engine.health()
                typer.echo(
                    f"[{state.status.value:>9}] {name} ({role}) — {state.detail} "
                    f"/ {len(state.models)} models"
                )
                for model in state.models:
                    typer.echo(f"      • {model}")
            models_dir = Path(cfg.inference.models_dir)
            ggufs = sorted(p.name for p in models_dir.glob("*") if p.suffix == ".gguf")
            typer.echo(f"models/ : {len(ggufs)} GGUF file(s)")
            for g in ggufs:
                typer.echo(f"      • {g}")
        finally:
            for engine in engines.values():
                await engine.aclose()

    asyncio.run(run())


@app.command("models")
def models(
    sync_ollama: bool = typer.Option(
        False, "--sync-ollama", help="Migrate local Ollama store models into models/"
    ),
    apply: bool = typer.Option(False, "--apply", help="Actually link files (default: dry run)"),
    mode: str = typer.Option("link", "--mode", help="link|copy"),
) -> None:
    """List discovered models; optionally sync the Ollama store into models/."""
    cfg = _config()
    from nomadicos.registry.ollama_store import discover, migrate, ollama_store_root
    from nomadicos.registry.scanner import scan_models_dir

    typer.echo(f"# models/ ({cfg.inference.models_dir})")
    for rec in scan_models_dir(cfg.inference.models_dir):
        typer.echo(f"  {rec.model_id:<52} src={rec.source} engine={rec.engine} roles={rec.roles}")

    if sync_ollama:
        try:
            root = ollama_store_root()
        except Exception as exc:
            typer.echo(f"ollama store unavailable: {exc}", err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"\n# ollama store at {root}")
        for info in discover(root):
            gb = info.size_bytes / 1e9
            if not apply:
                typer.echo(f"  [dry-run] {info.name} ({gb:.1f} GB)")
                continue
            path, how = migrate(
                info, cfg.inference.models_dir, mode="link" if mode == "link" else "copy"
            )
            typer.echo(f"  [{how:>8}] {info.name} ({gb:.1f} GB) -> {path}")


@app.command()
def permissions(
    action: str = typer.Argument(
        help="status | grant-full | revoke-all | add-instruction | pending | answer"
    ),
    arg1: str = typer.Argument("", help="rule text, conflict-id"),
    arg2: str = typer.Argument(
        "", help="resource pattern (add-instruction) or ALLOW/DENY (answer)"
    ),
) -> None:
    """Owner authority surface. Model/automation code must not call this."""
    from nomadicos.authority import AuthorityStore, AuthorizationService, CapabilityPolicy
    from nomadicos.kernel.events import EventLogger

    cfg = _config()
    store = AuthorityStore(Path(cfg.persistence.state_dir) / "authority.json")
    policy = CapabilityPolicy(
        store,
        granted_patterns=cfg.autonomy.capabilities,
        hard_denied_resources=cfg.autonomy.denied_resources,
    )
    svc = AuthorizationService(store, policy, EventLogger())

    if action == "status":
        state = store.state()
        typer.echo(f"epoch={state.epoch} grant={state.grant.profile if state.grant else 'NONE'}")
        if state.grant:
            typer.echo(f"granted_at={state.grant.granted_at} source={state.grant.source}")
        for ins in state.instructions:
            typer.echo(f"instruction: [{ins.id}] {ins.rule!r} resource={ins.resource!r}")
        return
    if action == "grant-full":
        svc.grant_full(source="owner_cli")
        typer.echo("FULL_PC_AUTONOMY granted (persistent).")
        return
    if action == "revoke-all":
        typer.echo(f"All access revoked; epoch now {svc.revoke_all()}.")
        return
    if action == "add-instruction":
        if not arg1:
            typer.echo("owner instruction text required", err=True)
            raise typer.Exit(2)
        ins = store.add_instruction(arg1, arg2)
        typer.echo(f"instruction {ins.id} recorded")
        return
    if action == "pending":
        for req in svc.pending_conflicts():
            typer.echo(f"{req.id}: {req.ask()}")
        return
    if action == "answer":
        svc.answer_conflict(arg1, arg2.upper(), resolver="owner")
        typer.echo(f"conflict {arg1}: {arg2.upper()} recorded")
        return
    typer.echo(f"unknown permissions action {action!r}", err=True)
    raise typer.Exit(2)


@app.command()
def run(
    goal: str = typer.Argument(help="Owner goal to execute through the task graph"),
    constraint: list[str] = typer.Option(  # noqa: B008 - typer idiom
        None, "--constraint", help="Explicit owner instruction/rule (repeatable)"
    ),
) -> None:
    """Run a goal through LangGraph: intake -> ... -> execute -> verify."""
    from nomadicos.orchestration.app import NomadicApp

    app = NomadicApp(_config())

    async def drive() -> None:
        try:
            await _drive_goal(app, goal, list(constraint or []))
        finally:
            await app.aclose()

    asyncio.run(drive())


def _render_run(summary) -> None:
    """Truthful reporting (SPEC §8.31): verdicts are separate from actions."""
    typer.echo(f"\nSTATUS: {summary.status.value}")
    if summary.outcome_note:
        typer.echo(f"NOTE:   {summary.outcome_note}")
    if summary.goal_verdict:
        typer.echo(f"GOAL:   {summary.goal_verdict} (verifier={summary.goal_verifier})")
        for line in summary.goal_why:
            typer.echo(f"        - {line}")
    typer.echo(
        f"TASK:   {summary.task_id} model={summary.model_id} "
        f"execs={summary.executions} recoveries={summary.recoveries}"
    )
    for obs in summary.last_observations:
        typer.echo(f"OBS:    {obs}")
    for fail in summary.failures:
        typer.echo(f"FAIL:   {fail.get('category')}: {str(fail.get('message'))[:120]}")


async def _drive_goal(app, goal: str, constraints: list[str]) -> None:
    """Shared driver: prompts the OWNER on WAITING_OWNER via authority resume."""
    from nomadicos.contracts.core import TaskStatus

    summary = await app.run_goal(goal, constraints=constraints)
    while summary.waiting_owner():
        typer.echo("\nSTATUS: WAITING_OWNER (your decision is required)")
        conflict = summary.conflict or {}
        typer.echo(f"CONFLICT: {conflict.get('conflicting_rule', '')}")
        typer.echo(
            f"REQUEST: {conflict.get('capability')} on {conflict.get('resource')!r} "
            f"(reason: {conflict.get('model_reason') or 'n/a'})"
        )
        answer = typer.prompt("OWNER DECISION", default="", show_default=False)
        summary = await app.resume_owner(summary.task_id, answer)
    _render_run(summary)
    if summary.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
        raise typer.Exit(1)


@app.command()
def code(
    goal: str = typer.Argument(help="Coding goal for the repository"),
    repo: str = typer.Option(".", "--repo", help="Owner-designated repository root"),
    test_command: str = typer.Option(  # noqa: B008
        "",
        "--test-command",
        help='Owner completion predicate: test command that must pass (e.g. "python -m pytest -q")',
    ),
    require_file: list[str] = typer.Option(  # noqa: B008
        None, "--require-file"
    ),
    require_contains: list[str] = typer.Option(  # noqa: B008
        None, "--require-contains", help="path:substring completion predicate"
    ),
) -> None:
    """Run a CODING task through the CodingWorker on the standard graph."""
    from nomadicos.orchestration.app import NomadicApp

    content_needles: list[tuple[str, str]] = []
    for item in require_contains or []:
        head, sep, rest = item.partition(":")
        if not sep:
            typer.echo("use --require-contains path:substring", err=True)
            raise typer.Exit(2)
        content_needles.append((head, rest))

    app = NomadicApp(_config())

    def ask(conflict: dict) -> str:
        typer.echo("\nSTATUS: WAITING_OWNER (editing a protected test file needs you)")
        typer.echo(
            f"REQUEST: {conflict.get('capability')} on {conflict.get('resource')!r} "
            f"-> {conflict.get('conflicting_rule')}"
        )
        while True:
            answer = typer.prompt("OWNER DECISION", default="", show_default=False).upper()
            if answer in {"ALLOW", "DENY"}:
                return answer

    async def drive() -> None:
        from nomadicos.contracts.core import TaskStatus

        try:
            await app.sync_models()
            summary, report = await app.run_coding(
                goal,
                repo=repo,
                test_command=test_command or None,
                require_files=list(require_file or []),
                require_content=content_needles,
                ask_owner=ask,
            )
            if summary.waiting_owner():
                report = None
            typer.echo(f"WORKER: {report.worker if report else 'coding-worker-v1'}")
            if report is not None:
                typer.echo(f"FILES:  wrote={report.files_written} deleted={report.files_deleted}")
                typer.echo(
                    f"TESTS:  runs={report.tests_run} exits={report.test_exits} "
                    f"repairs={report.repairs_used}"
                )
            _render_run(summary)
            if summary.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                raise typer.Exit(1)
        finally:
            await app.aclose()

    asyncio.run(drive())


@app.command("task")
def task_status(task_id: str = typer.Argument(help="task id printed by a previous run")) -> None:
    """Show the persisted summary of a task run."""
    from nomadicos.orchestration.app import NomadicApp

    summary = NomadicApp(_config()).status(task_id)
    if summary is None:
        typer.echo(f"no run record for {task_id!r}", err=True)
        raise typer.Exit(2)
    for key, value in summary.items():
        typer.echo(f"{key:>12}: {value}")


def main_entry() -> None:
    app()


if __name__ == "__main__":
    main_entry()
