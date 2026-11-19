"""Malformed worker output fails closed as InvalidProposal, never a crash (SPEC §16.6)."""

from __future__ import annotations

import json

import pytest

from nomadicos.kernel.errors import InvalidProposal
from nomadicos.orchestration.worker import WorkerAssignment, WorkerRole
from nomadicos.orchestration.workers import _parse_worker_result

_ASSIGNMENT = WorkerAssignment(
    role=WorkerRole.TESTING,
    task_id="t1",
    mission_id="m1",
    step_id="s1",
    objective="check calculator/index.html",
)


def _parse(obj: dict[str, object]):
    return _parse_worker_result(
        json.dumps(obj), worker="testing-worker-v1", role=WorkerRole.TESTING, assignment=_ASSIGNMENT
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"diagnostics": ["No test results discovered", "error handling not assessed"]},
        {"failure_category": ["ACTION_FAILED"]},
        {"elapsed_s": "soon"},
        {"artifacts": [{"not": "a string"}]},
    ],
)
def test_wrong_field_types_raise_invalid_proposal(bad: dict[str, object]) -> None:
    with pytest.raises(InvalidProposal):
        _parse({"status": "SUCCEEDED", **bad})


def test_valid_output_still_parses() -> None:
    result = _parse({"status": "SUCCEEDED", "diagnostics": "ok", "elapsed_s": 1.5})
    assert result.diagnostics == "ok"
