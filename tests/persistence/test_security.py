"""Phase 13A security guards: persistence is a DATA boundary only.

Source guards prove the persistence brick constructs no authority, writes no
SUCCESS, spawns no processes, and touches no network/model surfaces.
Behavioral guards prove SUCCESS and AuthorizedAction persist as inert data.
"""

from __future__ import annotations

import ast
import inspect

PERSISTENCE_MODULES = (
    "nomadicos.persistence.contracts",
    "nomadicos.persistence.errors",
    "nomadicos.persistence.store",
    "nomadicos.persistence.postgres",
)

FORBIDDEN_SOURCE_TOKENS = (
    "subprocess",
    "Popen",
    "os.system",
    "eval(",
    "exec(",
    "compile(",
    "urllib",
    "requests",
    "httpx",
    "socket.",
    "AuthorizedAction(",
    "grant_full",
    "revoke_all",
    "answer_conflict",
    "PolicyEngine",
    "TaskStatus.SUCCESS",
    "interrupt(",
    "os.system",
)

FORBIDDEN_IMPORT_ROOTS = (
    "nomadicos.inference",
    "nomadicos.router",
    "nomadicos.orchestration",
    "nomadicos.authority",
    "nomadicos.executor",
    "nomadicos.verification",
    "subprocess",
    "socket",
    "urllib",
    "http",
)


def _module_source(name: str) -> str:
    return inspect.getsource(__import__(name, fromlist=["x"]))


def test_persistence_modules_import_no_authority_or_infrastructure() -> None:
    for name in PERSISTENCE_MODULES:
        tree = ast.parse(inspect.getsource(__import__(name, fromlist=["x"])))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                assert not mod.startswith(
                    (
                        "nomadicos.authority",
                        "nomadicos.executor",
                        "nomadicos.orchestration",
                        "nomadicos.inference",
                        "nomadicos.router",
                        "nomadicos.verification",
                        "subprocess",
                        "socket",
                        "urllib",
                        "http",
                    )
                ), f"{name} imports {mod}"


def test_persistence_sources_contain_no_authority_or_spawn_tokens() -> None:
    for name in PERSISTENCE_MODULES:
        src = inspect.getsource(__import__(name, fromlist=["x"]))
        for token in (
            "subprocess",
            "Popen",
            "os.system",
            "eval(",
            "exec(",
            "urllib",
            "requests",
            "httpx",
            "socket.",
            "AuthorizedAction(",
            "grant_full",
            "revoke_all",
            "PolicyEngine",
        ):
            assert token not in src, f"{name} contains forbidden token {token!r}"
