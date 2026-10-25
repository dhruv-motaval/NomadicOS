"""Focused regression coverage (Phase 14C-G): resource-aware routing.

A candidate with a KNOWN resource requirement (declared vram_bytes /
ram_bytes) that exceeds the configured host budget is excluded. Either
side unknown = no constraint (deterministic cold-start fallback) — missing
resource information is never guessed and static size_hint_b is never
treated as VRAM/RAM usage. Concurrency/load and startup cost have NO
telemetry anywhere in the architecture, so no exclusion is fabricated for
them (documented deferral). Resource routing is never authoritative over
capability/quality semantics.
"""

from __future__ import annotations

from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.router import ModelSelector


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def reg_with(
    records: list[ModelRecord],
    samples: int = 3,
    success: bool = True,
    engine: str = "ollama",
) -> ModelRegistry:
    reg = ModelRegistry(EventLogger())
    for r in records:
        reg.register(r)
    for model_id in {r.model_id for r in records}:
        for _ in range(samples):
            reg.record_production(
                ProductionSample(
                    model_id=model_id, engine=engine, task_class="coding", success=success
                )
            )
    return reg


def ranked_ids(cfg: RouterConfig, reg: ModelRegistry) -> list[str]:
    return [c.model_id for c in ModelSelector(cfg).rank(reg, reqs())]


def model(mid: str, **over) -> ModelRecord:
    return ModelRecord(
        model_id=mid,
        engine="ollama",  # type: ignore[arg-type]
        roles=["coding"],
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        health=ModelHealth.HEALTHY,
        **over,
    )


# ------------------------------------------------------- VRAM filtering ----


def test_vram_over_budget_is_excluded() -> None:
    cfg = RouterConfig(max_vram_bytes=8_000_000_000)
    reg = reg_with(
        [
            model("small_vram", vram_bytes=4_000_000_000),
            model("big_vram", vram_bytes=24_000_000_000),
        ]
    )
    ids = ranked_ids(cfg, reg)
    assert "big_vram" not in ids  # known VRAM over budget: excluded
    assert "small_vram" in ids


def test_vram_within_budget_is_kept() -> None:
    cfg = RouterConfig(max_vram_bytes=32_000_000_000)
    reg = reg_with([model("fits", vram_bytes=24_000_000_000)])
    assert ranked_ids(cfg, reg) == ["fits"]


def test_ram_over_budget_is_excluded() -> None:
    cfg = RouterConfig(max_ram_bytes=16_000_000_000)
    reg = reg_with(
        [
            model("small_ram", ram_bytes=4_000_000_000),
            model("big_ram", ram_bytes=64_000_000_000),
        ]
    )
    ids = ranked_ids(cfg, reg)
    assert "big_ram" not in ids
    assert "small_ram" in ids


def test_missing_resource_information_falls_back_deterministically() -> None:
    # unknown vram_bytes/ram_bytes = no constraint: cold-start fallback
    cfg = RouterConfig(max_vram_bytes=1_000_000, max_ram_bytes=1_000_000)
    reg = reg_with([model("unknown_resources")])
    assert ranked_ids(cfg, reg) == ["unknown_resources"]
    # unknown host budget = no constraint either
    reg2 = reg_with([model("big", vram_bytes=999_000_000_000)])
    assert ranked_ids(RouterConfig(), reg2) == ["big"]


def test_resource_routing_never_overrides_capability_or_quality() -> None:
    cfg = RouterConfig(max_vram_bytes=32_000_000_000)
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id="caps_missing",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT],  # missing TOOL_USE/CODING
            vram_bytes=1_000_000,
        )
    )
    reg.register(model("caps_ok", vram_bytes=1_000_000))
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="caps_ok", engine="ollama", task_class="coding", success=False
            )
        )
    ids = ranked_ids(cfg, reg)
    # the low-resource but capability-less model is NOT selected just because
    # it fits; the capable model stays despite failing quality (floor: the
    # 3 failed samples DO gate it out, but never IN FAVOR of a caps-less model)
    assert "caps_missing" not in ids


def test_engine_specific_resource_differences() -> None:
    # the same model_id under two engines with different resource footprints
    cfg = RouterConfig(max_vram_bytes=8_000_000_000)
    reg = ModelRegistry(EventLogger())
    reg.register(model("X", vram_bytes=4_000_000_000))  # ollama footprint
    reg.register(
        ModelRecord(
            model_id="X",
            engine="llamacpp",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            vram_bytes=24_000_000_000,
        )
    )
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="caps_ok", engine="ollama", task_class="coding", success=False
            )
        )
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="X", engine="llamacpp", task_class="coding", success=True)
        )
    selected = ModelSelector(cfg).select(reg, reqs()).selected
    # the over-budget llamacpp footprint is excluded; ollama wins
    assert (selected.model_id, selected.engine) == ("X", "ollama")


def test_existing_router_regression_without_resource_fields() -> None:
    # no resource fields configured: routing is exactly the 14C-B behavior
    cfg = RouterConfig()
    reg = reg_with([model("X")])
    assert ranked_ids(cfg, reg) == ["X"]
