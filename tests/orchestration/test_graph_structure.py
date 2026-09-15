"""Graph construction and state-discipline tests (SPEC §7.26)."""

from __future__ import annotations

import inspect
from pathlib import Path

from helpers import make_app

from nomadicos.orchestration.graph import build_graph
from nomadicos.orchestration.state import FORBIDDEN_STATE_KEYS

EXPECT_NODES = {
    "intake",
    "classify",
    "plan",
    "select_model",
    "propose",
    "validate",
    "authorize",
    "owner_wait",
    "execute",
    "observe",
    "verify_step",
    "verify_goal",
    "advance",
    "recovery",
    "blocked",
    "failed",
    "record_partial",
}


def test_graph_compiles_with_all_nodes_and_key_edges(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    drawn = app.graph.get_graph()
    assert EXPECT_NODES <= set(drawn.nodes)
    edges = {(e.source, e.target) for e in drawn.edges}
    required = {
        ("__start__", "intake"),
        ("intake", "classify"),
        ("classify", "plan"),
        ("plan", "select_model"),
        ("execute", "observe"),
        ("observe", "verify_step"),
        ("advance", "select_model"),
        ("blocked", "__end__"),
        ("failed", "__end__"),
        ("record_partial", "__end__"),
    }
    assert required <= edges
    for source in (
        "select_model",
        "propose",
        "validate",
        "authorize",
        "owner_wait",
        "verify_step",
        "verify_goal",
        "recovery",
    ):
        outgoing = {dst for src, dst in edges if src == source}
        assert outgoing, f"{source} has no outgoing routing"


def test_graph_is_deterministic_across_builds(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    a = {n for n in app.graph.get_graph().nodes}
    b = {n for n in build_graph(app.runtime, checkpointer=None).get_graph().nodes}
    assert a == b


def test_orchestration_graph_never_touches_os_or_network_directly() -> None:
    """No bypasses: graph.py must not spawn processes or hit FS/network."""
    import nomadicos.orchestration.graph as graph_module

    src = inspect.getsource(graph_module)
    for forbidden in (
        "subprocess",
        "os.system",
        "os.remove",
        "os.unlink",
        "shutil",
        "httpx",
        "write_text",
        "open(",
    ):
        assert forbidden not in src, f"graph reached raw OS/network: {forbidden}"
    # and the executor is the single dispatch point the graph uses
    assert "runtime.executor.execute" in src


def test_state_schema_has_no_authority_shaped_keys() -> None:
    from nomadicos.orchestration.state import TaskState

    fields = {name.lower() for name in TaskState.__annotations__}
    assert not (fields & set(FORBIDDEN_STATE_KEYS))
    assert "pending_authorized_id" in fields  # references only
    assert "authorized_action" not in fields
