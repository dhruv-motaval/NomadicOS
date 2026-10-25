"""Focused regression coverage (Phase 14C-D): difficulty + latency-aware
routing.

The Phase 14C routing audit CONFIRMED that TaskRequirements.latency and
difficulty were computed but effectively unused. 14C-D makes the task's
latency intent (FAST/BALANCED/QUALITY, derived from difficulty by the
analyzer) drive the quality gate, and wires the configured global routing
mode into the analyzer as the base (graph classify node) - so the task
intent, not only the global mode, rules candidate selection.
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
from nomadicos.router.selection import _quality_gate


def reqs(latency: str = "BALANCED", difficulty: float = 0.6) -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=difficulty,
        latency=latency,  # type: ignore[arg-type]
    )


def reg_with(
    samples: int,
    success: bool,
    engine: str = "ollama",
    model_id: str = "X",
    context_window: int = 8192,
    extra_caps: list[CapabilityTag] | None = None,
) -> ModelRegistry:
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id=model_id,
            engine=engine,  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
            + (extra_caps or []),
            health=ModelHealth.HEALTHY,
            context_window=context_window,
        )
    )
    for _ in range(samples):
        reg.record_production(
            ProductionSample(model_id=model_id, engine=engine, task_class="coding", success=success)
        )
    return reg


def ranked_ids(cfg: RouterConfig, reg: ModelRegistry, r: TaskRequirements) -> list[str]:
    return [c.model_id for c in ModelSelector(cfg).rank(reg, r)]


# ----------------------------------------------------------- gate intent ----


def test_quality_gate_follows_task_latency_intent() -> None:
    cfg = RouterConfig()
    assert cfg.mode == "BALANCED"
    # the TASK's latency intent rules: QUALITY task under a BALANCED global
    # mode uses the QUALITY gate (0.85), not the lenient global one
    assert _quality_gate(cfg, "QUALITY") == 0.85
    assert _quality_gate(cfg, "BALANCED") == 0.7
    assert _quality_gate(cfg, "FAST") == 0.55


def test_quality_task_not_gated_by_global_mode_alone() -> None:
    cfg = RouterConfig()  # global BALANCED (0.7)
    # measured success 0.75: passes the BALANCED gate but FAILS QUALITY
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id="X",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
        )
    )
    for success in (True, True, True, False):
        reg.record_production(
            ProductionSample(model_id="X", engine="ollama", task_class="coding", success=success)
        )
    # BALANCED task: 0.75 >= 0.7 - kept
    assert "X" in ranked_ids(cfg, reg, reqs(latency="BALANCED"))
    # QUALITY task (hard, difficulty 0.8): 0.75 < 0.85 - gate excludes
    assert "X" not in ranked_ids(cfg, reg, reqs(latency="QUALITY", difficulty=0.8))


def test_fast_task_keeps_lenient_gate() -> None:
    cfg = RouterConfig()  # global BALANCED
    reg2 = ModelRegistry(EventLogger())
    reg2.register(
        ModelRecord(
            model_id="Y",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
        )
    )
    for success in (True, False, True, False, True):  # 0.6 over 5 samples: gate active
        reg2.record_production(
            ProductionSample(model_id="Y", engine="ollama", task_class="coding", success=success)
        )
    # BALANCED intent: 0.6 < 0.7 - gate excludes
    assert "Y" not in ranked_ids(cfg, reg2, reqs(latency="BALANCED"))
    # FAST intent: 0.6 >= 0.55 - the lenient FAST gate keeps the candidate
    assert "Y" in ranked_ids(cfg, reg2, reqs(latency="FAST", difficulty=0.2))


def test_difficulty_influences_routing_via_intent() -> None:
    # the analyzer derives the intent from difficulty: >= 0.7 -> QUALITY
    from nomadicos.contracts.core import Goal
    from nomadicos.router.analyzer import analyze_goal

    hard = analyze_goal(Goal(objective="implement a complex multi-module architecture change"))
    easy = analyze_goal(Goal(objective="write file notes.txt"))
    assert hard.latency == "QUALITY" and hard.difficulty >= 0.7
    assert easy.difficulty < 0.7
    cfg = RouterConfig()  # global BALANCED
    # large-context task + analyzer-derived TESTING requirement: the model
    # carries the required window and capabilities
    testing = [CapabilityTag.TESTING]
    reg = reg_with(samples=4, success=True, context_window=65536, extra_caps=testing)
    reg2 = reg_with(
        samples=4, success=False, model_id="Z", context_window=65536, extra_caps=testing
    )
    assert "X" in ranked_ids(cfg, reg, hard)
    assert "Z" not in ranked_ids(cfg, reg2, hard)


def test_fast_global_mode_still_compatible() -> None:
    cfg = RouterConfig(mode="FAST")
    assert _quality_gate(cfg, "FAST") == 0.55
    # an easy task under global FAST inherits the FAST base via the analyzer
    reg = reg_with(samples=3, success=True)
    assert "X" in ranked_ids(cfg, reg, reqs(latency="FAST", difficulty=0.2))
    # a hard task under global FAST is gated by its QUALITY intent (0.85):
    # a hard task never routes on the lenient global gate alone
    assert _quality_gate(cfg, "QUALITY") == 0.85


def test_ranking_remains_deterministic_across_intents() -> None:
    cfg = RouterConfig()
    reg = reg_with(samples=3, success=True)
    for latency in ("FAST", "BALANCED", "QUALITY"):
        r = reqs(latency=latency, difficulty=0.2 if latency == "FAST" else 0.6)
        ids = ranked_ids(cfg, reg, r)
        assert ids == ["X"]
        assert ids == ranked_ids(cfg, reg, r)  # deterministic per intent
