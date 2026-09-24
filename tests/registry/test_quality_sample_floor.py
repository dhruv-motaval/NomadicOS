"""Focused regression coverage (Phase 14C-B): minimum sample floor for
quality gating.

The Phase 14C routing audit CONFIRMED the single-sample distortion: a model
with ONE measured sample below minimum_quality was excluded immediately
(selection.py quality gate). 14C-B makes the gate reject a measured
candidate ONLY when the evidence is sufficient (samples >=
RouterConfig.minimum_sample_count). No success rate is invented or
smoothed; score/role_fit/sorting/engine/version semantics are unchanged;
N=1 reproduces the previous always-gate behavior.
"""

from __future__ import annotations

from nomadicos.contracts.model import CapabilityTag, ModelRecord, TaskRequirements, TaskType
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.registry.model_registry import aggregate_production
from nomadicos.router import ModelSelector

DEFAULT_MIN_QUALITY = RouterConfig().minimum_quality["BALANCED"]  # 0.7


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def reg_with(
    samples: int, success: bool, engine: str = "ollama", model_id: str = "X"
) -> ModelRegistry:
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id=model_id,
            engine=engine,  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    for _ in range(samples):
        reg.record_production(
            ProductionSample(model_id=model_id, engine=engine, task_class="coding", success=success)
        )
    return reg


def ranked_ids(cfg: RouterConfig, reg: ModelRegistry) -> list[str]:
    return [c.model_id for c in ModelSelector(cfg).rank(reg, reqs())]


# ------------------------------------------------------------ gate floor ----


def test_one_failed_sample_does_not_exclude_when_N_gt_1() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    assert cfg.minimum_sample_count == 3
    reg = reg_with(samples=1, success=False)
    bundle = reg.bundle_for("X", "ollama", "coding")
    # the measured evidence itself is unchanged: honest 0.0 over 1 sample
    assert bundle.samples == 1
    assert bundle.success_rate == 0.0
    assert DEFAULT_MIN_QUALITY == 0.7
    # samples 1 < N 3: evidence insufficient for the gate - NOT excluded
    assert "X" in ranked_ids(cfg, reg)


def test_single_sample_is_insufficient_for_gating_either_way() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    ok = reg_with(samples=1, success=True, model_id="X_ok")
    bad = reg_with(samples=1, success=False, model_id="X_bad")
    # one successful sample: measured, ranked - the gate never applied
    assert ok.bundle_for("X_ok", "ollama", "coding").success_rate == 1.0
    assert ok.bundle_for("X_ok", "ollama", "coding").samples == 1
    # one failed sample: measured, ranked - the gate never applied either
    assert bad.bundle_for("X_bad", "ollama", "coding").success_rate == 0.0
    # neither is excluded nor privileged by the gate at samples < N
    assert "X_ok" in ranked_ids(cfg, ok)
    assert "X_bad" in ranked_ids(cfg, bad)


def test_exactly_N_samples_activates_the_quality_gate() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    # exactly N failing samples: gate ACTIVE - excluded (success 0.0 < 0.7)
    bad = reg_with(samples=3, success=False)
    assert bad.bundle_for("X", "ollama", "coding").samples == 3
    assert "X" not in ranked_ids(cfg, bad)
    # exactly N passing samples: gate ACTIVE - kept (success 1.0 >= 0.7)
    ok = reg_with(samples=3, success=True, model_id="X_ok")
    assert "X_ok" in ranked_ids(cfg, ok)


def test_N_equals_1_reproduces_previous_behavior() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    assert cfg.minimum_sample_count == 1
    # one failed sample: samples 1 >= N 1 - gate APPLIES, excluded (old)
    bad = reg_with(samples=1, success=False)
    assert "X" not in ranked_ids(cfg, bad)
    # three failed samples: excluded as before
    bad3 = reg_with(samples=3, success=False, model_id="X3")
    assert "X3" not in ranked_ids(cfg, bad3)
    # one passing sample: kept as before
    ok = reg_with(samples=1, success=True, model_id="X_ok")
    assert "X_ok" in ranked_ids(cfg, ok)


def test_high_success_at_N_minus_1_remains_insufficient() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    reg = reg_with(samples=2, success=True)
    bundle = reg.bundle_for("X", "ollama", "coding")
    assert bundle.samples == 2
    assert bundle.success_rate == 1.0
    assert bundle == aggregate_production(
        [
            ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True),
            ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True),
        ]
    )
    # N-1 samples: the gate never applied; the candidate is ranked and the
    # sole candidate is selectable (not failed closed on thin evidence)
    ranked = ModelSelector(cfg).rank(reg, reqs())
    assert [c.model_id for c in ranked] == ["X"]
    assert ranked[0].measured is True
    assert ModelSelector(cfg).select(reg, reqs()).selected.model_id == "X"


# -------------------------------------------- isolation + cold start -------


def test_engine_isolation_remains_intact() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id="X",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    # ollama: 1 failed sample (insufficient -> ranked);
    # llamacpp: 3 failed samples (sufficient -> gate excludes)
    reg.record_production(
        ProductionSample(model_id="X", engine="ollama", task_class="coding", success=False)
    )
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="X", engine="llamacpp", task_class="coding", success=False)
        )
    assert reg.bundle_for("X", "ollama", "coding").samples == 1
    assert reg.bundle_for("X", "llamacpp", "coding").samples == 3
    # the single engine-scoped record registers one candidate; its ollama
    # evidence is insufficient for the gate, so the candidate stays ranked
    assert "X" in ranked_ids(cfg, reg)


def test_benchmark_version_isolation_remains_intact() -> None:
    from nomadicos.contracts.benchmark import BenchmarkRecord

    cfg = RouterConfig(minimum_sample_count=3)
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id="X",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )

    def bench(version: str, success: bool) -> BenchmarkRecord:
        return BenchmarkRecord(
            id=f"bench_{version}_{int(success)}",
            model_id="X",
            engine_id="ollama",  # type: ignore[arg-type]
            benchmark_version=version,
            task_id="t",
            task_category="bug_fix",  # type: ignore[arg-type]
            language="python",
            difficulty=0.3,
            success=success,
        )

    # v1: 2 failing samples (insufficient at N=3); v2: 3 failing samples
    # (sufficient -> gate active). Version-scoped bundles are evaluated
    # separately; the floor never merges versions.
    for _ in range(2):
        reg.record_benchmark(bench("bench-v1", False))
    for _ in range(3):
        reg.record_benchmark(bench("bench-v2", False))
    v1 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v1", prefer="benchmark")
    v2 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v2", prefer="benchmark")
    assert v1.samples == 2 and v1.success_rate == 0.0
    assert v2.samples == 3 and v2.success_rate == 0.0
    # the newest-version (v2, sufficient) bundle is the one under the gate:
    # against a bug_fix-class lookup the candidate stays cold-start (the
    # benchmark category and routing task_class vocabularies are distinct),
    # and no version blending occurred
    ranked = ModelSelector(cfg).rank(reg, reqs())
    scored = [c for c in ranked if c.model_id == "X"]
    assert scored and scored[0].measured is False


def test_cold_start_remains_deterministic() -> None:
    cfg = RouterConfig(minimum_sample_count=3)
    reg = reg_with(samples=0, success=False)
    bundle = reg.bundle_for("X", "ollama", "coding")
    assert bundle.samples == 0
    assert bundle.success_rate is None  # nothing invented
    ranked = ModelSelector(cfg).rank(reg, reqs())
    assert [c.model_id for c in ranked] == ["X"]
    assert ranked[0].measured is False
    # deterministic across repeated ranks
    assert [c.model_id for c in ModelSelector(cfg).rank(reg, reqs())] == ["X"]


def test_default_floor_fixes_single_sample_distortion() -> None:
    # the shipped default (N=3) already prevents the audit's distortion
    reg = reg_with(samples=1, success=False)
    assert "X" in ranked_ids(RouterConfig(), reg)
