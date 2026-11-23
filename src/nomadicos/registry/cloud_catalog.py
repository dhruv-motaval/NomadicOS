"""Sync the cloud engine's normalized catalog into the model registry
(SPEC §10, §12). Only OWNER-ALLOWED models are registered; each record keeps
its catalog metadata so the router and traces can cite provider, pricing and
the catalog timestamp. Registration is metadata — it grants nothing."""

from __future__ import annotations

import re

from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord, ModelRole
from nomadicos.inference.cloud.engine import CloudEngine
from nomadicos.inference.cloud.policy import matches_any, qualified_id
from nomadicos.kernel.config import CloudConfig
from nomadicos.registry.model_registry import ModelRegistry

_SIZE = re.compile(r"(?:^|[-/_:.])(\d+(?:\.\d+)?)b(?:$|[-/_:.])", re.IGNORECASE)
_FALLBACK_CONTEXT = 8192  # conservative placeholder; meta.context_window keeps the truth


def sync_cloud_catalog(registry: ModelRegistry, engine: CloudEngine, cfg: CloudConfig) -> int:
    """Register allowlisted catalog entries (health HEALTHY only for a
    provider whose last refresh succeeded) and drop cloud records the
    catalog no longer lists. Returns the number of cloud records now held."""
    fresh = {
        qid: meta for qid, meta in engine.catalog.items() if matches_any(qid, cfg.allowed_models)
    }
    for record in registry.all():
        if record.engine == "cloud" and record.model_id not in fresh:
            registry.remove(record.model_id, "cloud")
    for qid, meta in fresh.items():
        caps = [CapabilityTag.TEXT]
        if meta.structured_output:  # the Brain's proposals are structured JSON
            caps.append(CapabilityTag.TOOL_USE)
        if {"reasoning", "include_reasoning"} & set(meta.supported_parameters):
            caps.append(CapabilityTag.REASONING)
        roles: list[ModelRole] = [r for r, pats in cfg.roles.items() if matches_any(qid, pats)]  # type: ignore[misc]
        size = _SIZE.search(meta.provider_model_id)
        status = engine.catalog_status.get(meta.provider)
        registry.register(
            ModelRecord(
                model_id=qualified_id(meta.provider, meta.provider_model_id),
                engine="cloud",
                roles=roles,
                capabilities=caps,
                context_window=meta.context_window or _FALLBACK_CONTEXT,
                health=ModelHealth.HEALTHY if status == "ok" else ModelHealth.UNKNOWN,
                source="cloud",
                location=meta.provider,
                params={"size_hint_b": float(size.group(1))} if size else {},
                cloud=meta,
            )
        )
    return len(fresh)
