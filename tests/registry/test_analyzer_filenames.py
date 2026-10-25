"""Focused regression coverage: deterministic task analyzer filename
false-positive.

The bug: the _CODING regex contained bare "test", so the filename
"test.txt" matched \btest\b — a simple file-creation task became CODING
with a TESTING requirement no model satisfies, producing RESOURCE_UNAVAILABLE.
The fix introduces explicit testing-intent detection: bare "test"/"tests"
inside filenames (test.txt, test.py, tests.json) never triggers testing
classification, while legitimate testing requests are preserved.
"""

from __future__ import annotations

import pytest

from nomadicos.contracts.core import Goal
from nomadicos.contracts.model import CapabilityTag, TaskType
from nomadicos.router.analyzer import analyze_goal


def Ggoal(text: str) -> Goal:  # noqa: N802 - helper
    return Goal(objective=text)


# ------------------------------------------------ filename false-positive --


@pytest.mark.parametrize(
    "filename", ["test.txt", "test.py", "tests.json", "test_data.md", "run_tests.log"]
)
def test_filename_tokens_never_trigger_testing(filename: str) -> None:
    req = analyze_goal(Ggoal(f"Create {filename} containing NOMADICOS_TEST"))
    # file work with an extension target: never coding/testing
    assert req.task_type is TaskType.SIMPLE_FILE
    assert CapabilityTag.CODING not in req.capabilities
    assert CapabilityTag.TESTING not in req.capabilities


def test_create_test_txt_classifies_as_simple_file() -> None:
    req = analyze_goal(Ggoal("Create test.txt containing NOMADICOS_TEST"))
    # the EXACT reported bug: SIMPLE_FILE with text + tool_use only
    assert req.task_type is TaskType.SIMPLE_FILE
    assert req.capabilities == [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]


def test_create_benchmark_output_txt_is_simple_file() -> None:
    # the 14B suite objective phrasing is file work, not terminal/coding
    req = analyze_goal(Ggoal("Create benchmark_output.txt containing exactly NOMADICOS_14B_PASS"))
    assert req.task_type is TaskType.SIMPLE_FILE
    assert CapabilityTag.CODING not in req.capabilities


# ---------------------------------------------------- legitimate testing ---


@pytest.mark.parametrize(
    "objective",
    [
        "run tests",
        "run the test suite",
        "execute pytest",
        "make the tests pass",
        "fix the failing tests",
    ],
)
def test_legitimate_testing_requests_keep_testing_intent(objective: str) -> None:
    req = analyze_goal(Ggoal(objective))
    assert CapabilityTag.TESTING in req.capabilities


def test_fix_failing_tests_is_coding_with_testing() -> None:
    req = analyze_goal(Ggoal("fix the failing tests"))
    assert req.task_type is TaskType.CODING
    assert CapabilityTag.CODING in req.capabilities
    assert CapabilityTag.TESTING in req.capabilities


def test_add_feature_and_run_tests_preserved() -> None:
    # the existing spec-example behavior is preserved
    req = analyze_goal(Ggoal("Add a feature and run tests."))
    assert req.task_type is TaskType.CODING
    assert CapabilityTag.TOOL_USE in req.capabilities
    assert CapabilityTag.TESTING in req.capabilities


def test_predicate_driven_testing_preserved() -> None:
    goal = Goal.from_spec(
        "Get the app working",
        predicates=[{"type": "tests_pass", "command": "pytest"}],
    )
    req = analyze_goal(goal)
    # the tests_pass predicate drives the CODING branch (previous behavior)
    assert req.verification_required is True
    assert req.task_type is TaskType.CODING
    assert CapabilityTag.CODING in req.capabilities


def test_analyzer_remains_deterministic() -> None:
    a = analyze_goal(Ggoal("Create test.txt containing NOMADICOS_TEST"))
    b = analyze_goal(Ggoal("Create test.txt containing NOMADICOS_TEST"))
    assert a == b
