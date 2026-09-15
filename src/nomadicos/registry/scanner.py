"""Registry discovery: models dir scan + engine sync (SPEC §12, models/ contract).

The owner drops model files into ``models/``; here they become registry
records. Engines remain the authority on what is served.
"""

from __future__ import annotations

import re
from pathlib import Path

from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.inference.base import InferenceEngine
from nomadicos.kernel.config import ModelRoles
from nomadicos.registry.model_registry import ModelRegistry

MODEL_FILE_EXTENSIONS = {".gguf", ".safetensors"}

_ROLE_CAPS: dict[str, list[CapabilityTag]] = {
    "tiny": [CapabilityTag.TEXT],
    "worker": [CapabilityTag.TEXT, CapabilityTag.TOOL_USE],
    "coding": [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    "reasoning": [CapabilityTag.TEXT, CapabilityTag.REASONING, CapabilityTag.TOOL_USE],
    "critic": [CapabilityTag.TEXT, CapabilityTag.REASONING, CapabilityTag.CODING],
    "evaluator": [CapabilityTag.TEXT, CapabilityTag.REASONING, CapabilityTag.TESTING],
}

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)b", re.IGNORECASE)


def estimate_size_b(model_id_or_file: str) -> float | None:
    """Best-effort parameter size from the name (e.g. qwen3:14b -> 14.0)."""
    match = _SIZE_RE.search(Path(model_id_or_file).name.replace("-", ""))
    return float(match.group(1)) if match else None


def caps_for_roles(roles: list[str]) -> list[CapabilityTag]:
    caps: list[CapabilityTag] = []
    for role in roles:
        for cap in _ROLE_CAPS.get(role, []):
            if cap not in caps:
                caps.append(cap)
    return caps


def roles_from_filename(name: str) -> list[str]:
    parts = {p.strip() for p in re.split(r"[-_\s:.]", name.lower()) if p}
    roles: list[str] = []
    if parts & {"coder", "codereview", "coding"}:
        roles.append("coding")
    if parts & {"critic", "judge", "evaluator"}:
        roles.append("critic")
    if parts & {"tiny", "mini", "0.5b", "1.5b", "1.7b", "3b"}:
        roles.append("tiny")
    if not roles:
        roles.append("worker")
    return roles


def scan_models_dir(dir_path: str | Path, *, engine: str = "llamacpp") -> list[ModelRecord]:
    """Register owner-placed model files. Unmeasured health stays UNKNOWN."""
    root = Path(dir_path)
    if not root.exists():
        return []
    records: list[ModelRecord] = []
    for file in sorted(root.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in MODEL_FILE_EXTENSIONS:
            continue
        size_hint = estimate_size_b(file.name)
        roles = roles_from_filename(file.name)
        records.append(
            ModelRecord(
                model_id=f"models/{file.name}",
                engine=engine,  # type: ignore[arg-type]
                roles=roles,  # type: ignore[arg-type]
                capabilities=caps_for_roles(roles),
                context_window=int((size_hint or 4) * 2048) if size_hint else 8192,
                health=ModelHealth.UNKNOWN,
                source="models_dir",
                location=str(file),
                params={"size_hint_b": size_hint} if size_hint else {},
            )
        )
    return records


class RegistryBuilder:
    """Combines config-declared roles, engine listings, and dir scan."""

    def __init__(self, roles: ModelRoles) -> None:
        self._roles = roles

    def _role_for(self, model_id: str) -> list[str]:
        pairs: list[tuple[str, str | None]] = [
            ("tiny", self._roles.tiny),
            ("worker", self._roles.worker),
            ("coding", self._roles.coding_worker),
            ("coding", self._roles.coding_escalation),
            ("reasoning", self._roles.reasoning),
            ("critic", self._roles.critic),
        ]
        roles = [role for role, configured in pairs if configured == model_id]
        return sorted(set(roles)) if roles else roles_from_filename(model_id)

    def build(
        self,
        registry: ModelRegistry,
        *,
        models_dir: str | Path | None = None,
        default_dir_engine: str = "llamacpp",
    ) -> ModelRegistry:
        if models_dir is not None:
            for record in scan_models_dir(models_dir, engine=default_dir_engine):
                configured = self._role_for(record.model_id)
                enriched = record.model_copy(update={"roles": configured})
                registry.register(enriched)
        return registry

    async def sync_from_engine(
        self, registry: ModelRegistry, engine: InferenceEngine
    ) -> ModelRegistry:
        """Register models an engine reports, enriched by config roles."""
        health = await engine.health()
        for model in health.models:
            roles = self._role_for(model)
            registry.register(
                ModelRecord(
                    model_id=model,
                    engine=engine.engine_id,  # type: ignore[arg-type]
                    roles=roles,  # type: ignore[arg-type]
                    capabilities=caps_for_roles(roles),
                    health=health.status,
                    source=engine.engine_id,  # type: ignore[arg-type]
                )
            )
        return registry


__all__ = [
    "MODEL_FILE_EXTENSIONS",
    "RegistryBuilder",
    "caps_for_roles",
    "estimate_size_b",
    "roles_from_filename",
    "scan_models_dir",
]
