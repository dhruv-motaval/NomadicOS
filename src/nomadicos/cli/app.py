"""NomadicOS command-line entry point (SPEC §40). Thin by contract."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="NomadicOS — local-first AI OS.")


def _config():
    from nomadicos.kernel.config import load_config

    return load_config()


def _owner_presence(cfg):
    from nomadicos.authority.presence import presence_from_config

    return presence_from_config(cfg.autonomy.owner_presence)


def _confirm_or_refuse(cfg, purpose: str) -> bool:
    """Widening owner decisions need the owner in person (SPEC §56.5)."""
    from nomadicos.authority.presence import OwnerPresenceNotConfirmed, require_owner_presence

    try:
        require_owner_presence(_owner_presence(cfg), purpose)
    except OwnerPresenceNotConfirmed as exc:
        typer.echo(f"refused: {exc.message}", err=True)
        return False
    return True


def _prompt_owner_decision(cfg, purpose: str) -> str:
    """ALLOW counts only once the owner is confirmed in person; DENY needs
    no proof. Anything else asks again."""
    while True:
        answer = typer.prompt("OWNER DECISION", default="", show_default=False).strip().upper()
        if answer == "DENY":
            return answer
        if answer == "ALLOW" and _confirm_or_refuse(cfg, purpose):
            return answer


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
    from nomadicos.tools.terminal import TOOL_PROCESS_ENV

    # Actions that WIDEN authority are refused for a process the terminal
    # brick spawned (SPEC §4, §56.5); restricting ones (revoke, add a rule)
    # stay available to anyone.
    if action in ("grant-full", "answer") and os.environ.get(TOOL_PROCESS_ENV):
        typer.echo("refused: a NomadicOS task process cannot change owner authority", err=True)
        raise typer.Exit(3)

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
        if not _confirm_or_refuse(cfg, "grant FULL_PC_AUTONOMY"):
            raise typer.Exit(3)
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
        if arg2.upper() == "ALLOW" and not _confirm_or_refuse(cfg, f"ALLOW conflict {arg1[:40]}"):
            raise typer.Exit(3)
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
    predicate: list[str] = typer.Option(  # noqa: B008 - typer idiom
        None,
        "--predicate",
        help='Completion check as JSON, e.g. \'{"type":"file_exists","path":"a.txt"}\' '
        "(repeatable; all must hold for SUCCESS)",
    ),
) -> None:
    """Run a goal through LangGraph: intake -> ... -> execute -> verify."""
    import json

    from nomadicos.contracts.core import parse_predicate
    from nomadicos.orchestration.app import NomadicApp

    predicates = []
    for raw in predicate or []:
        try:
            parsed = json.loads(raw)
            parse_predicate(parsed)  # fail here, before any engine starts
        except ValueError as exc:
            typer.echo(f"invalid --predicate {raw!r}: {exc}", err=True)
            raise typer.Exit(2) from exc
        predicates.append(parsed)

    app = NomadicApp(_config())

    async def drive() -> None:
        try:
            await app.sync_models()  # live engine models + models/ files (SPEC §12)
            await _drive_goal(app, goal, list(constraint or []), predicates)
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
    # Critic transcript: evaluative only. SUCCESS is never reported here -
    # only the STATUS/GOAL lines above carry completion meaning (SPEC §10.34).
    for it in summary.critic_iterations[-3:]:
        mark = " (ACCEPT suppressed: evidence rule)" if it.get("accept_suppressed") else ""
        typer.echo(
            f"CRITIC: {it.get('decision')} score={it.get('score')} "
            f"model={it.get('model_id')} iteration={it.get('iteration')}{mark}"
        )
    typer.echo(
        f"TASK:   {summary.task_id} model={summary.model_id} "
        f"execs={summary.executions} recoveries={summary.recoveries}"
    )
    for obs in summary.last_observations:
        typer.echo(f"OBS:    {obs}")
    for fail in summary.failures:
        typer.echo(f"FAIL:   {fail.get('category')}: {str(fail.get('message'))[:120]}")


async def _drive_goal(
    app, goal: str, constraints: list[str], predicates: list | None = None
) -> None:
    """Shared driver: prompts the OWNER on WAITING_OWNER via authority resume."""
    from nomadicos.contracts.core import TaskStatus

    summary = await app.run_goal(goal, constraints=constraints, predicates=predicates or None)
    while summary.waiting_owner():
        typer.echo("\nSTATUS: WAITING_OWNER (your decision is required)")
        conflict = summary.conflict or {}
        typer.echo(f"CONFLICT: {conflict.get('conflicting_rule', '')}")
        typer.echo(
            f"REQUEST: {conflict.get('capability')} on {conflict.get('resource')!r} "
            f"(reason: {conflict.get('model_reason') or 'n/a'})"
        )
        answer = _prompt_owner_decision(app.config, f"ALLOW a blocked action ({summary.task_id})")
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
    critic: str = typer.Option(  # noqa: B008
        "", "--critic", help="Ollama-registered model id used for the CRITIC role (evaluation only)"
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
        return _prompt_owner_decision(app.config, "ALLOW editing a protected test file")

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
                critic_model=critic or None,
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


@app.command("tasks")
def tasks() -> None:
    """List durable tasks (Phase 13C discovery)."""
    from nomadicos.orchestration.app import NomadicApp

    entries = NomadicApp(_config()).list_durable_tasks()
    if not entries:
        typer.echo("no durable tasks")
        return
    for entry in entries:
        typer.echo(f"  {entry['task_id']}  status={entry['status']}  ({entry['source']})")


@app.command()
def resume(task_id: str = typer.Argument(help="durable task id to resume")) -> None:
    """Safely resume a durable task through the normal pipeline (Phase 13C)."""
    from nomadicos.orchestration.app import NomadicApp

    cfg = _config()

    async def drive() -> None:
        app = NomadicApp(cfg)
        try:
            summary = await app.resume_task(task_id)
            _render_run(summary)
        finally:
            await app.aclose()

    asyncio.run(drive())


def main_entry() -> None:
    app()


if __name__ == "__main__":
    main_entry()
