"""Policy loading — fail closed (ADR-0013, BP §85).

Invalid, missing-required, or invariant-violating policy artifacts abort startup
(BP §236: security initialization failure ⇒ SAFE MODE, never permissive).
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from nomadicos.constitution.policy_schema import ExternalNetworkPolicy, PolicyDocument
from nomadicos.core.errors import SecurityPolicyViolation
from nomadicos.core.logging import get_logger

logger = get_logger("constitution")


class PolicyEngine:
    """Owns the loaded policy documents; read-only view for the rest of the system.

    The model may *see* relevant policy constraints (BP §260) but nothing outside
    the owner can mutate this engine — `replace_policy` is an owner action only.
    """

    def __init__(self) -> None:
        self._documents: list[PolicyDocument] = []

    @property
    def documents(self) -> tuple[PolicyDocument, ...]:
        return tuple(self._documents)

    @property
    def versioned(self) -> bool:
        return bool(self._documents)

    def load_file(self, path: str | Path) -> PolicyDocument:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SecurityPolicyViolation(
                f"unparseable policy file: {path.name} ({type(exc).__name__})",
                context={"path": path.name},
            ) from exc
        if not isinstance(raw, dict):
            raise SecurityPolicyViolation(
                f"policy file must be a mapping: {path.name}",
                context={"path": path.name},
            )
        try:
            document = PolicyDocument.model_validate(raw)
        except ValidationError as exc:
            # Fail closed: never coerce, never default permissive (BP §85).
            raise SecurityPolicyViolation(
                f"invalid policy file: {path.name}",
                context={"path": path.name, "errors": exc.error_count()},
            ) from exc
        self._documents.append(document)
        logger.info("policy loaded version=%s source=%s", document.version, path.name)
        return document

    def load_directory(self, directory: str | Path) -> list[PolicyDocument]:
        directory = Path(directory)
        if not directory.exists():
            raise SecurityPolicyViolation(
                "policy directory missing",
                context={"directory": directory.name},
            )
        loaded = [
            self.load_file(path)
            for path in sorted(directory.glob("*.yaml"))
            if path.is_file()
        ]
        if not loaded:
            raise SecurityPolicyViolation(
                "no policy files found — refusing to run without policy",
                context={"directory": directory.name},
            )
        return loaded

    def replace_policy(self, document: PolicyDocument) -> None:
        """Owner action (BP §259): replaces an existing document version,
        only after schema + invariant validation already succeeded."""
        self._documents = [
            d for d in self._documents if d.version != document.version
        ]
        self._documents.append(document)
        logger.info("policy replaced version=%s", document.version)

    def rules_for(self, tool_name: str) -> list:
        rules: list = []
        for document in self._documents:
            rules.extend(document.rules_for(tool_name))
        return rules

    def decision_for(
        self, tool_name: str, risk, has_user_authorization: bool = False
    ) -> str:
        from nomadicos.constitution.policy_schema import resolve_decision

        return resolve_decision(self.rules_for(tool_name), risk, has_user_authorization)

    def external_network(self) -> ExternalNetworkPolicy | None:
        documents = [d for d in self._documents if d.owner.external_network]
        if not documents:
            return None
        policy = documents[0].owner.external_network
        # Fail closed: if any loaded document forbids, it is forbidden (BP §262).
        if not all(d.owner.external_network.allow_public_get for d in documents):
            policy = policy.model_copy(update={"allow_public_get": False})
        return policy


__all__ = ["PolicyEngine"]
