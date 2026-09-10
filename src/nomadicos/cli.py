"""NomadicOS CLI (ADR-0015): task intake + control, talking to the same
Agent Runtime interface the future UI will use (BP §78, §121)."""

import argparse
import asyncio
import sys

from nomadicos.core.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nomadicos", description="NomadicOS v0.1 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("task", help="task operations")
    task_sub = create.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create", help="create and run a task")
    task_create.add_argument("goal", help="task goal, e.g. 'Open VS Code and run the tests'")

    sub.add_parser("status", help="show subsystem status")
    sub.add_parser("stop", help="emergency stop (BP §122)")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    from nomadicos.core.runtime import Runtime

    runtime = Runtime()

    if args.command == "status":
        print(runtime.status_line())
        return 0

    if args.command == "stop":
        runtime.emergency_stop()
        print("EMERGENCY STOP engaged (BP §122) — active tasks halted")
        return 0

    if args.command == "task" and args.task_command == "create":
        report = asyncio.run(runtime.run_goal(args.goal))
        print(report.render())
        return 0 if report.status.value in ("SUCCESS", "PARTIALLY_COMPLETED") else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
