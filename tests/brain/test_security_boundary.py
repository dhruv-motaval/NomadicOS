"""Security boundary of the MAP core: no authority creation, no execution
paths, no policy mutation, and a hard import boundary (Sprint 2;
NomadicBrain spec §24; NomadicOS SPEC §56)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nomadicos.brain import contracts as brain_contracts
from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
)
from nomadicos.brain.loop import PlanningOutcome, PlanningStatus
from nomadicos.brain.monitor import MonitorDecision, MonitorResult, MonitorStage
from nomadicos.brain.schemas import ActionCandidate, CandidateKind
from nomadicos.contracts.action import AuthorizedAction

ALLOWED_ROOTS = {"nomadicos.brain", "nomadicos.contracts", "nomadicos.kernel"}
FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(?:nomadicos\.(?:executor|authority|tools|inference|orchestration|"
    r"router|registry|persistence|action_ir|agents|evaluation|api|cli|"
    r"memory|desktop|kernel\.events)|"
    r"httpx|http|openai|anthropic|requests|aiohttp|ollama|llama_cpp|"
    r"socket|subprocess)\b",
    re.M,
)


def _brain_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "src" / "nomadicos" / "brain"


def _brain_files() -> list[Path]:
    return sorted((_brain_dir()).rglob("*.py"))


def test_brain_contracts_expose_no_authority_types() -> None:
    """No AuthorizedAction / ActionProposal / grant types on the brain
    surface: the cognitive layer cannot create or carry authority."""
    exported = set(brain_contracts.__all__)
    assert "AuthorizedAction" not in exported
    assert "ActionProposal" not in exported
    assert not hasattr(brain_contracts, "AuthorizedAction")
    assert not hasattr(brain_contracts, "AuthorityStore")
    assert not hasattr(brain_contracts, "AuthorizationService")
    assert not hasattr(brain_contracts, "Executor")


def test_brain_modules_import_only_their_boundary() -> None:
    """Brain modules may import ONLY brain, contracts, and kernel — no
    executor, authority, tools, inference, orchestration, or providers."""
    files = _brain_files()
    assert files
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert not FORBIDDEN_IMPORT.search(source), (
            f"{path.name} imports an execution/network/provider primitive"
        )
        for module in re.findall(
            r"^\s*(?:from|import)\s+(nomadicos\.[A-Za-z_][\w.]*)", source, re.M
        ):
            root = ".".join(module.split(".")[:2])
            assert root in ALLOWED_ROOTS, f"{path.name} imports {module!r}"


def test_prompts_do_not_live_in_the_brain_domain() -> None:
    """Prompt wording belongs to the inference adapter, never the brain."""
    for path in sorted(_brain_dir().glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "reply with ONLY one JSON object" not in source, (
            f"{path.name} embeds a prompt template"
        )


class _FakeGenerator:
    async def generate_structured(self, request: CognitiveRequest) -> str:
        return "{}"


def test_structured_generator_is_the_only_model_seam() -> None:
    """The brain's only model seam is the StructuredGenerator protocol."""
    assert isinstance(_FakeGenerator(), StructuredGenerator)
    assert brain_contracts.StructuredGenerator is StructuredGenerator


def test_cognitive_request_rejects_authority_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        CognitiveRequest(
            role=CognitiveRole.ACTOR,
            mission_id="m",
            payload={},
            authorized=True,  # type: ignore[call-overload]
        )


def test_monitor_result_rejects_authority_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MonitorResult(
            candidate_id="cand_c1",
            decision=MonitorDecision.ACCEPTED,
            stage=MonitorStage.SEMANTIC,
            permission="terminal.write",  # type: ignore[call-overload]
        )


def test_planning_outcome_carries_no_authority() -> None:
    with pytest.raises(Exception):  # noqa: B017 - extra="forbid"
        PlanningOutcome(
            status=PlanningStatus.ACCEPTED,
            iterations_used=1,
            accepted_candidate_ids=["cand_c1"],
            capability_grant="terminal.*",  # type: ignore[call-overload]
        )


def test_actor_proposal_candidates_cannot_be_authorized_actions() -> None:
    """Candidates carry no authorization and cannot become AuthorizedAction."""
    candidate = ActionCandidate(
        id="cand_c1",
        kind=CandidateKind.EXECUTE,
        tool="terminal",
        expected_effect="e",
    )
    dump = candidate.model_dump()
    assert not ({"authorized", "owner_approved", "bypass_policy"} & set(dump))
    with pytest.raises(Exception):  # noqa: B017 - ValidationError expected
        AuthorizedAction.model_validate(dump)
