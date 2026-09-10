"""Tests for the interactive CLI REPL (ADR-0015)."""


from nomadicos.cli_interactive import InteractiveCLI


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)


class FakeInput:
    """Scripted stdin: pops queued lines, raises EOF when empty."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __call__(self, prompt: str = "") -> str:
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


class FakeRuntime:
    """Minimal Runtime surface for REPL tests."""

    def __init__(self) -> None:
        self.goals: list[str] = []
        self.status_text = "NomadicOS | test"
        self.sync_calls = 0

    def run_goal_sync(self, goal, user_id="local-owner"):
        from nomadicos.agent.runtime import TaskReport
        from nomadicos.core.lifecycle import TaskStatus

        self.goals.append(goal)
        return TaskReport(
            task_id="t-1",
            goal=goal,
            status=TaskStatus.SUCCESS,
            requested=goal,
            completed=[f"did: {goal}"],
        )

    def recent_tasks(self, limit=10):
        from datetime import datetime

        return [
            {
                "created_at": datetime(2026, 9, 7, 10, 0),
                "status": "SUCCESS",
                "goal": "Create hello.txt",
            }
        ]

    async def sync_fleet(self, force=False):
        self.sync_calls += 1
        return {"added": 0, "removed": 0, "changed": 0, "healthy": 0, "failed": 0}

    async def register_ollama_models(self):
        return 0

    @property
    def manager(self):
        class M:
            def list_available(self):
                return []

        return M()

    @property
    def memory(self):
        class Mem:
            async def search(self, query, limit=5):
                return []

        return Mem()

    def status_line(self):
        return self.status_text


def make_cli(lines: list[str]) -> tuple[InteractiveCLI, Recorder, FakeRuntime]:
    out = Recorder()
    runtime = FakeRuntime()
    cli = InteractiveCLI(runtime, input_fn=FakeInput(lines), output_fn=out)
    return cli, out, runtime


def test_chat_message_executes_task() -> None:
    cli, out, runtime = make_cli(["Create hello.txt with a greeting", "/close"])
    exit_code = cli.run()
    assert exit_code == 0
    assert runtime.goals == ["Create hello.txt with a greeting"]
    assert any("SUCCESS" in line for line in out.lines)


def test_multiple_messages_sequence() -> None:
    cli, out, runtime = make_cli(["first goal", "second goal", "/close"])
    cli.run()
    assert runtime.goals == ["first goal", "second goal"]


def test_slash_close_exits() -> None:
    cli, out, runtime = make_cli(["/close"])
    assert cli.run() == 0


def test_slash_help_does_not_execute_task() -> None:
    cli, out, runtime = make_cli(["/help", "/close"])
    cli.run()
    assert runtime.goals == []


def test_unknown_command_reported() -> None:
    cli, out, runtime = make_cli(["/bogus", "/close"])
    cli.run()
    assert any("Unknown command" in line for line in out.lines)


def test_sessions_shows_history() -> None:
    cli, out, runtime = make_cli(["/sessions", "/close"])
    cli.run()
    assert any("Create hello.txt" in line for line in out.lines)


def test_eof_exits_gracefully() -> None:
    cli, out, runtime = make_cli([])
    assert cli.run() == 0


def test_empty_lines_ignored() -> None:
    cli, out, runtime = make_cli(["", "   ", "/close"])
    cli.run()
    assert runtime.goals == []


def test_failed_task_is_reported_not_crashed() -> None:
    class FailingRuntime(FakeRuntime):
        def run_goal_sync(self, goal, user_id="local-owner"):
            raise RuntimeError("model unavailable")

    out = Recorder()
    runtime = FailingRuntime()
    cli = InteractiveCLI(runtime, input_fn=FakeInput(["break it", "/close"]), output_fn=out)
    cli.run()
    assert any("model unavailable" in line for line in out.lines)
