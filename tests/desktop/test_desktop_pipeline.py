"""Phase 12A pipeline proof: desktop actions travel the EXISTING path only —
IR -> validation -> authorization -> AuthorizedAction -> Executor -> tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomadicos.action_ir import (
    ProposalValidator,
    ValidationContext,
    parse_model_output,
)
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.policy import CapabilityPolicy
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.executor import Executor
from nomadicos.kernel.errors import AuthorizationDenied, InvalidProposal, RevokedAuthority
from nomadicos.kernel.events import EventLogger
from nomadicos.tools import (
    DesktopTool,
    ExecutionContext,
    FilesystemTool,
    ProcessSupervisor,
    TerminalTool,
    ToolRegistry,
)

PATTERNS = ["filesystem.*", "terminal.*", "git.*", "process.*", "desktop.*"]


class DesktopChain:
    """The production pipeline wired once, with the desktop tool registered."""

    def __init__(self, tmp_path: Path, backend: FakeDesktopBackend | None = None) -> None:
        self.log = EventLogger()
        self.store = AuthorityStore(tmp_path / "state" / "authority.json")
        self.backend = backend or FakeDesktopBackend(width=640, height=480)
        reg = ToolRegistry()
        reg.register(FilesystemTool())
        reg.register(TerminalTool(ProcessSupervisor()))
        reg.register(DesktopTool(self.backend))
        self.tools = reg
        self.validator = ProposalValidator(reg, self.log)
        policy = CapabilityPolicy(self.store, granted_patterns=list(PATTERNS))
        self.authz = AuthorizationService(self.store, policy, self.log)
        self.executor = Executor(reg, self.store, self.log)

    def ctx(self, tmp_path: Path, task_id: str = "task_alpha") -> ExecutionContext:
        return ExecutionContext.for_task(task_id, tmp_path / "ws")

    def parse(self, raw: str, ctx: ExecutionContext):
        return parse_model_output(
            raw, task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
        )

    def validate(self, parsed, ctx: ExecutionContext):
        return self.validator.validate(
            parsed,
            ValidationContext(
                task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
            ),
        )

    async def drive(self, raw: str, ctx: ExecutionContext):
        parsed = self.parse(raw, ctx)
        if parsed.is_claim:
            return parsed.value
        authorized = self.authz.authorize(parsed.value, self.validate(parsed.value, ctx))
        return await self.executor.execute(authorized, ctx)


def desktop_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "desktop", "operation": operation, "args": args})


def granted_chain(tmp_path: Path, backend: FakeDesktopBackend | None = None) -> DesktopChain:
    chain = DesktopChain(tmp_path, backend)
    chain.store.grant_full_pc_autonomy()
    return chain


# ------------------------------------------------------------ full path ----


async def test_desktop_action_travels_the_existing_pipeline(tmp_path) -> None:
    backend = FakeDesktopBackend(width=640, height=480)
    chain = granted_chain(tmp_path, backend)
    ctx_ = chain.ctx(tmp_path)
    result = await chain.drive(
        desktop_json("mouse_move", {"x": 12, "y": 34}),
        ctx_,
    )
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.tool == "desktop" and result.operation == "mouse_move"
    assert result.evidence == {"x": 12, "y": 34, "screen": [640, 480]}
    assert backend.cursor == (12, 34)


async def test_window_listing_through_full_pipeline(tmp_path) -> None:
    chain = granted_chain(tmp_path)
    result = await chain.drive(desktop_json("list_windows", {}), chain.ctx(tmp_path))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert "Untitled - Notepad" in result.evidence["titles"]


# ------------------------------------------------------- fail-closed gates -


async def test_unknown_desktop_operation_fails_closed(tmp_path) -> None:
    chain = granted_chain(tmp_path)
    with pytest.raises(InvalidProposal):
        await chain.drive(desktop_json("reformat_everything", {}), chain.ctx(tmp_path))
    assert not [e for e in chain.log.events("task_alpha") if e.type.value == "TOOL_EXECUTED"]


async def test_no_grant_denies_desktop_actions(tmp_path) -> None:
    chain = DesktopChain(tmp_path)
    chain.store.revoke_all()
    with pytest.raises(AuthorizationDenied):
        await chain.drive(
            desktop_json("mouse_move", {"x": 1, "y": 2}), chain.ctx(tmp_path)
        )
    assert chain.backend.cursor == (0, 0)


async def test_revocation_kills_authorized_desktop_action(tmp_path) -> None:
    chain = granted_chain(tmp_path)
    ctx_ = chain.ctx(tmp_path)
    parsed = chain.parse(desktop_json("list_windows", {}), ctx_)
    authorized = chain.authz.authorize(parsed.value, chain.validate(parsed.value, ctx_))
    chain.store.revoke_all()
    with pytest.raises(RevokedAuthority):
        await chain.executor.execute(authorized, ctx_)


async def test_model_authored_authority_fields_are_rejected(tmp_path) -> None:
    chain = granted_chain(tmp_path)
    hostile = json.dumps(
        {
            "tool": "desktop",
            "operation": "mouse_move",
            "args": {"x": 1, "y": 2, "authorized": True},
        }
    )
    with pytest.raises(InvalidProposal):
        await chain.drive(hostile, chain.ctx(tmp_path))
    assert chain.backend.cursor == (0, 0)


# ------------------------------------------------- desktop success != goal -


async def test_desktop_step_success_writes_no_success(tmp_path) -> None:
    chain = granted_chain(tmp_path)
    ctx_ = chain.ctx(tmp_path)
    await chain.drive(desktop_json("list_windows", {}), ctx_)
    results = [e.result for e in chain.log.events(ctx_.task_id)]
    assert "SUCCESS" not in results
