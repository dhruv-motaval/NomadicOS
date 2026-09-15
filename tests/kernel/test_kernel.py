"""Kernel: ids, errors, events, config (SPEC §30, §35, §42)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.kernel.config import AppConfig, load_config
from nomadicos.kernel.errors import (
    AuthorizationDenied,
    BudgetExhausted,
    ConfigInvalid,
    Failure,
    InvalidProposal,
    NomadicOSBaseError,
    UnknownCapability,
)
from nomadicos.kernel.events import Event, EventBus, EventLogger, EventType
from nomadicos.kernel.ids import new_id


def test_ids_unique_prefixed() -> None:
    a, b = new_id("task"), new_id("task")
    assert a != b
    assert a.startswith("task_")
    with pytest.raises(ValueError):
        new_id("bogus")


def test_error_taxonomy_matches_spec_30() -> None:
    expected = {
        "INVALID_PROPOSAL",
        "AUTHORIZATION_DENIED",
        "ACTION_FAILED",
        "TIMEOUT",
        "TOOL_ERROR",
        "MODEL_ERROR",
        "VERIFICATION_FAILED",
        "GOAL_NOT_SATISFIED",
        "RESOURCE_UNAVAILABLE",
    }
    assert expected <= {f.value for f in Failure}
    assert InvalidProposal("x").failure is Failure.INVALID_PROPOSAL
    assert UnknownCapability("x").failure is Failure.AUTHORIZATION_DENIED
    assert issubclass(UnknownCapability, AuthorizationDenied)


def test_error_context_and_to_dict() -> None:
    err = BudgetExhausted("nope", task_id="task_1", budget="max_steps")
    d = err.to_dict()
    assert d["category"] == "BUDGET_EXHAUSTED"
    assert d["context"]["task_id"] == "task_1"
    assert isinstance(err, NomadicOSBaseError)


def test_event_carries_correlation_fields() -> None:
    ev = Event(
        EventType.ACTION_PROPOSED,
        task_id="task_1",
        step_id="step_1",
        attempt=2,
        model_id="m",
        tool="terminal",
        capability="terminal.execute",
        result="OK",
        payload={"x": 1},
    )
    d = ev.to_dict()
    assert d["type"] == "ACTION_PROPOSED"
    assert d["task_id"] == "task_1"
    assert d["attempt"] == 2
    assert d["payload"] == {"x": 1}
    assert d["timestamp"]


def test_event_bus_fanout_and_isolation() -> None:
    bus = EventBus()
    logger = EventLogger(bus=bus)
    seen: list[str] = []

    def good(ev: Event, _l: EventLogger) -> None:
        seen.append(ev.id)

    def bad(ev: Event, _l: EventLogger) -> None:
        raise RuntimeError("subscriber boom")

    bus.subscribe(good)
    bus.subscribe(bad, EventType.TASK_CREATED)
    task_ev = Event(EventType.TASK_CREATED, task_id="t1")
    logger.publish(task_ev)
    # subscriber failure does not prevent other handlers nor raise
    assert task_ev.id in seen
    assert any(e.type is EventType.VERIFICATION_RESULT for e in logger.events())


def test_event_logger_sink_forwarding() -> None:
    forwarded: list[Event] = []
    logger = EventLogger(sink=forwarded.append)
    logger.log(EventType.TASK_CREATED, task_id="t1")
    assert len(forwarded) == 1


def test_event_logger_sink_failure_swallowed() -> None:
    def broken(_e: Event) -> None:
        raise RuntimeError("audit store down")

    logger = EventLogger(sink=broken)
    ev = logger.log(EventType.TOOL_EXECUTED, task_id="t1")
    assert ev.to_dict()["task_id"] == "t1"


def test_config_defaults_select_local_bricks() -> None:
    cfg = AppConfig()
    assert cfg.inference.default_engine == "llamacpp"
    assert cfg.inference.fallback_engine == "ollama"
    assert cfg.inference.models_dir == "models"
    assert cfg.autonomy.profile == "FULL_PC_AUTONOMY"
    assert cfg.orchestration.engine == "langgraph"
    assert cfg.verification.require_goal_proof is True


def test_config_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "inference:\n  default_engine: ollama\n  fallback_engine: mock\n"
        "models:\n  worker: ollama/ornith\nbudget:\n  max_recoveries: 5\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.inference.default_engine == "ollama"
    assert cfg.models.worker == "ollama/ornith"
    assert cfg.budget.max_recoveries == 5


def test_missing_config_file_is_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.yaml") == AppConfig()


def test_invalid_config_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("inference:\n  default_engine: gpt-cloud\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        load_config(p)
    p.write_text("inference: 5\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        load_config(p)


def test_goal_proof_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"verification": {"require_goal_proof": False}})


def test_unknown_capability_namespace_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"autonomy": {"capabilities": ["nuclear.launch"]}})


def test_same_engine_conflict_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"inference": {"default_engine": "ollama", "fallback_engine": "ollama"}}
        )
