"""Deterministic benchmark suite (Phase 14B, SPEC §52A).

Repository-owned standardized tasks with STABLE identities: benchmark ids
and the suite version are declared constants - never derived from UUIDs,
timestamps, temp paths, or randomness. Every task reuses the existing
supported task types (contracts/benchmark.py BenchmarkTask) and the
existing supported predicate vocabulary of the verification brick.

Category ↔ task-type relationship (existing abstractions, not renamed):
``category`` is the BenchmarkCategory Literal; the task NATURE (filesystem,
terminal, coding, planning, verification/failure) is expressed by the
objective and the goal predicates the model must satisfy.

Declared expectation semantics: a positive benchmark succeeds ONLY through
the GoalVerifier (goal_verdict PASS); a negative benchmark stays honestly
negative (NOT_VERIFIED / NOT_PASS) and is never turned into SUCCESS.
"""

from __future__ import annotations

from typing import Any

from nomadicos.contracts.benchmark import BenchmarkTask

#: stable suite version - bumped only by an explicit suite revision
BENCHMARK_VERSION = "1"

SUITE: tuple[BenchmarkTask, ...] = (
    BenchmarkTask(
        id="bench_fs_create_001",
        category="feature_implementation",
        objective="Create benchmark_output.txt containing exactly NOMADICOS_14B_PASS",
        difficulty=0.2,
        workspace_files={"seed_notes.txt": "benchmark seed fixture"},
        goal_predicates=[
            {
                "type": "file_content_equals",
                "path": "benchmark_output.txt",
                "content": "NOMADICOS_14B_PASS",
            },
        ],
    ),
    BenchmarkTask(
        id="bench_fs_transform_001",
        category="refactoring",
        objective="Copy the full content of seed_notes.txt into a new file seed_copy.txt",
        difficulty=0.3,
        workspace_files={"seed_notes.txt": "seed content alpha"},
        goal_predicates=[
            {"type": "file_exists", "path": "seed_copy.txt"},
            {"type": "file_contains", "path": "seed_copy.txt", "text": "seed content alpha"},
        ],
    ),
    BenchmarkTask(
        id="bench_term_exit_001",
        category="debugging",
        objective="Run a terminal probe command and confirm it exits with code 0",
        difficulty=0.3,
        goal_predicates=[{"type": "exit_code_equals", "code": 0}],
    ),
    BenchmarkTask(
        id="bench_code_fix_001",
        category="bug_fix",
        objective="Fix calc.py so the file contains exactly: result = 1 + 2",
        difficulty=0.4,
        workspace_files={"calc.py": "result = 1 +\n"},
        goal_predicates=[
            {"type": "file_content_equals", "path": "calc.py", "content": "result = 1 + 2"},
        ],
    ),
    # ------------------------------------------------------- negatives -----
    BenchmarkTask(
        id="bench_neg_unverifiable_001",
        category="repair",
        objective="Write summary.txt reporting the repair result",
        difficulty=0.3,
        workspace_files={"broken.txt": "broken"},
        # default verification expectation: the task never runs pytest, so
        # the GoalVerifier stays honestly NOT_VERIFIED (expected failure)
        goal_predicates=[],
    ),
    BenchmarkTask(
        id="bench_neg_missing_target_001",
        category="code_review",
        objective="Review seed_notes.txt and write review.md",
        difficulty=0.3,
        workspace_files={"seed_notes.txt": "code to review"},
        # the goal demands a file the task never creates: deterministic
        # NOT_PASS (expected failure), never manufactured into SUCCESS
        goal_predicates=[{"type": "file_exists", "path": "review_missing_target.md"}],
    ),
)

#: declared expected verification semantics per benchmark (SPEC §52A):
#: positive ⇒ GoalVerifier PASS; negative ⇒ honest NOT_VERIFIED/NOT_PASS.
EXPECTED: dict[str, dict[str, Any]] = {
    task.id: {
        "expected_success": not task.id.startswith("bench_neg_"),
        "expectation": "goal_pass" if not task.id.startswith("bench_neg_") else "goal_not_verified",
    }
    for task in SUITE
}


def suite_ids() -> list[str]:
    """Stable benchmark identities (deterministic order)."""
    return [task.id for task in SUITE]


def get_task(benchmark_id: str) -> BenchmarkTask | None:
    """Suite loader: the declared task for a stable benchmark id."""
    for task in SUITE:
        if task.id == benchmark_id:
            return task
    return None


def expected_for(benchmark_id: str) -> dict[str, Any]:
    """The declared expected verification semantics for a benchmark."""
    return EXPECTED.get(
        benchmark_id, {"expected_success": False, "expectation": "unknown_benchmark"}
    )
