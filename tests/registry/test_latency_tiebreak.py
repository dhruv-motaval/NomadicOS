"""Focused regression coverage (Phase 14C-E): measured-latency tiebreak.

The Phase 14C routing audit CONFIRMED that median/p95 latency aggregates
were computed but unused. 14C-E adds a deterministic latency tiebreak among
otherwise-equivalent candidates. Documented precedence:
    measured-first -> size -> score -> median latency -> p95 -> model_id
Latency NEVER overrides capability, health, quality gate, benchmark-version
isolation, or engine isolation; unknown latency sorts last (never invented).
"""

from __future__ import annotations

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry
from nomadicos.router import ModelSelector


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def reg_with_critter(
    mid: str, success: bool, latency_ms: float | None, engine: str = "ollama"
) -> ModelRegistry:
    """One model, one measured benchmark record with explicit latency."""
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id=mid,
            engine=engine,  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            params={"size_hint_b": 7.0},  # identical size for tiebreak tests
        )
    )
    rec = BenchmarkRecord(
        id=f"bench_{mid}_{int(success)}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version="bench-v1",
        task_id="t",
        task_category="bug_fix",  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=success,
        total_latency_ms=latency_ms,
    )
    reg.record_benchmark(rec)
    return reg


def ranked(cfg: RouterConfig, reg: ModelRegistry) -> list[str]:
    return [c.model_id for c in ModelSelector(cfg).rank(reg, reqs())]


# ------------------------------------------------- latency tiebreak --------


def test_equal_quality_same_size_latency_tiebreak() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    # identical size proxy, identical success (1.0), different latency:
    # the FASTER measured candidate wins the tiebreak
    reg = ModelRegistry(EventLogger())
    for mid, lat in (("fast_model", 100.0), ("slow_model", 900.0)):
        reg.register(
            ModelRecord(
                model_id=mid,
                engine="ollama",  # type: ignore[arg-type]
                roles=["coding"],
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": 7.0},
            )
        )
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_{mid}",
                model_id=mid,
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=True,
                total_latency_ms=lat,
            )
        )
    assert ranked(cfg, reg) == ["fast_model", "slow_model"]


def test_latency_does_not_override_score() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    for mid, success, lat in (
        ("high_score", True, 900.0),
        ("low_score", False, 10.0),
    ):
        reg.register(
            ModelRecord(
                model_id=mid,
                engine="ollama",  # type: ignore[arg-type]
                roles=["coding"],
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": 7.0},
            )
        )
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_{mid}",
                model_id=mid,
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=success,
                total_latency_ms=lat,
            )
        )
    # the high-score (1.0) candidate outranks the fast-but-failing one:
    # latency never overrides the score/quality ordering
    assert ranked(cfg, reg).index("high_score") < ranked(cfg, reg).index("low_score")


def test_latency_does_not_override_size_order() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    for mid, size, lat in (
        ("big_model", 30.0, 10.0),
        ("small_model", 7.0, 900.0),
    ):
        reg.register(
            ModelRecord(
                model_id=mid,
                engine="ollama",  # type: ignore[arg-type]
                roles=["coding"],
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": size},
            )
        )
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_{mid}",
                model_id=mid,
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=True,
                total_latency_ms=lat,
            )
        )
    # smallest-capable-first is preserved: fast-but-big loses to slow-but-small
    assert ranked(cfg, reg) == ["small_model", "big_model"]


def test_missing_latency_sorts_last_among_equals() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    for mid, lat in (("measured", 100.0), ("unmeasured_latency", None)):
        reg.register(
            ModelRecord(
                model_id=mid,
                engine="ollama",  # type: ignore[arg-type]
                roles=["coding"],
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": 7.0},
            )
        )
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_{mid}",
                model_id=mid,
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=True,
                total_latency_ms=lat,
            )
        )
    # unknown latency sorts last among otherwise-equivalent candidates
    # (never invented, never guessed) - deterministic
    assert ranked(cfg, reg) == ["measured", "unmeasured_latency"]
    assert ranked(cfg, reg) == ranked(cfg, reg)


def test_p95_tiebreak_when_medians_equal() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    # medians equal (both 500ms), p95 differs: p95 breaks the second tie
    reg.register(
        ModelRecord(
            model_id="tight_p95",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            params={"size_hint_b": 7.0},
        )
    )
    for lat in (400.0, 500.0, 600.0):  # median 500, p95 600
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_tight_{lat}",
                model_id="tight_p95",
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=True,
                total_latency_ms=lat,
            )
        )
    reg.register(
        ModelRecord(
            model_id="wild_p95",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            params={"size_hint_b": 7.0},
        )
    )
    for lat in (500.0, 500.0, 5000.0):  # median 500, p95 5000
        reg.record_benchmark(
            BenchmarkRecord(
                id=f"bench_wild_{lat}",
                model_id="wild_p95",
                engine_id="ollama",  # type: ignore[arg-type]
                benchmark_version="bench-v1",
                task_id="t",
                task_category="bug_fix",  # type: ignore[arg-type]
                language="python",
                difficulty=0.3,
                success=True,
                total_latency_ms=lat,
            )
        )
    # equal size, equal score (1.0), equal median (0.5s): tighter p95 wins
    assert ranked(cfg, reg) == ["tight_p95", "wild_p95"]


def test_unmeasured_candidates_unaffected_by_latency() -> None:
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    reg.register(
        ModelRecord(
            model_id="unmeasured",
            engine="ollama",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            params={"size_hint_b": 7.0},
        )
    )
    # no records at all: unmeasured cold start, no latency invented
    ranked_list = ModelSelector(cfg).rank(reg, reqs())
    assert [c.model_id for c in ranked_list] == ["unmeasured"]
    assert ranked_list[0].measured is False
    assert ranked_list[0].median_latency_s is None
    assert ranked_list[0].p95_latency_s is None


def test_existing_router_regression_unchanged() -> None:
    # without latency evidence the ordering is exactly the 14C-B behavior:
    # measured-first, then size, then score, then model_id
    cfg = RouterConfig(minimum_sample_count=1)
    reg = ModelRegistry(EventLogger())
    for mid, size in (("small", 7.0), ("big", 30.0)):
        reg.register(
            ModelRecord(
                model_id=mid,
                engine="ollama",  # type: ignore[arg-type]
                roles=["coding"],
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": size},
            )
        )
    assert ranked(cfg, reg) == ["small", "big"]  # smallest capable first
