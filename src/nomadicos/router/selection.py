"""Model selection: deterministic pipeline ending in the smallest capable
model that satisfies the task (SPEC §13, §52B, §56.11-12).

    candidates -+-> capability match -> health/resource filter ->
                quality+historical gate -> size-minimal selection
"""

from __future__ import annotations

from pydantic import Field

from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import (
    CapabilityTag,
    MetricsBundle,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.registry.scanner import estimate_size_b
from nomadicos.router.quality import (
    estimate_memory_mib,
    reliability,
    task_bucket,
    utility,
)

_SIZELESS_MODEL = 7.0  # neutral size proxy when no hint exists


class CandidateScore(Contract):
    model_id: str
    stage: str = "qualified"
    score: float
    size_b: float
    measured: bool
    reason: str = ""
    #: exact engine identity of the candidate record (Phase 14C-F)
    engine: str = ""
    #: measured latency tiebreak signals (Phase 14C-E), None = unmeasured
    median_latency_s: float | None = None
    p95_latency_s: float | None = None
    #: Phase 14D: conservative verified-success estimate (Wilson LCB), None =
    #: unmeasured (never presented as known reliability)
    p_lcb: float | None = None
    #: Phase 14D: quality-constrained utility score, None = excluded from the
    #: quality path (falls back to the deterministic size-based policy)
    utility: float | None = None


class RankedCandidate(Contract):
    """A quality-path candidate: the same hard filters as ``rank()``, plus a
    conservative reliability estimate and a utility score over latency and
    memory. Selection among these is efficiency-first AFTER the reliability
    floor (SPEC §14D)."""

    model_id: str
    engine: str = ""
    stage: str = "qualified"
    utility: float = 0.0
    p_lcb: float = 0.0
    size_b: float = 0.0
    measured: bool = False
    median_latency_s: float | None = None
    p95_latency_s: float | None = None
    memory_mib: float | None = None
    reason: str = ""


class SelectionResult(Contract):
    selected: ModelRecord
    requirements_key: str
    ranked: list[CandidateScore] = Field(default_factory=list)
    #: Phase 14D: the quality-constrained candidate chain, highest utility
    #: first. Empty when no benchmark evidence exists for the bucket — the
    #: caller then falls back to the deterministic size-based policy.
    quality_candidates: list[RankedCandidate] = Field(default_factory=list)


def _size_proxy(record: ModelRecord) -> float:
    hint = record.params.get("size_hint_b")
    if isinstance(hint, (int, float)):
        return float(hint)
    return estimate_size_b(record.model_id) or _SIZELESS_MODEL


def _quality_gate(router: RouterConfig, task_latency: str | None = None) -> float:
    """Effective quality gate (Phase 14C-D): the task's latency intent
    (FAST/BALANCED/QUALITY, derived from difficulty by the analyzer) rules;
    the global router mode is the fallback. A QUALITY-classified task never
    routes on a lenient global gate; a FAST task keeps its lenient gate."""
    if task_latency is not None:
        return router.minimum_quality.get(task_latency, 0.7)
    return router.minimum_quality.get(router.mode, 0.7)


class ModelSelector:
    def __init__(self, router: RouterConfig) -> None:
        self._router = router

    def _hard_filter(
        self, registry: ModelRegistry, requirements: TaskRequirements
    ) -> tuple[str, list[tuple[ModelRecord, MetricsBundle]]]:
        """Shared hard-constraint filter (SPEC §14D-1).

        Returns ``(task_class, qualified)``. The filters here are never
        scoring preferences — a model lacking a required capability, or
        exceeding a known host budget, must not win on speed alone.
        """
        task_class = requirements.task_type.value
        specialist_caps = set(requirements.capabilities) - {
            CapabilityTag.TEXT,
            CapabilityTag.TOOL_USE,
        }
        # Once any model has explicit evidence for a specialist capability,
        # that evidence becomes the qualification source for the task. Before
        # that point, declared capabilities remain a deterministic cold-start
        # fallback, but are never presented as measured evidence.
        measured_caps = {
            capability
            for capability in specialist_caps
            if any(
                registry.capability_bundle(record.model_id, record.engine, capability).samples
                >= self._router.minimum_sample_count
                for record in registry.enabled()
            )
        }
        evidence_required = specialist_caps & measured_caps
        qualified: list[tuple[ModelRecord, MetricsBundle]] = []
        for record in registry.enabled():
            if record.health in (ModelHealth.UNHEALTHY, ModelHealth.UNKNOWN):
                # health filter: only PROVEN-runnable engine state is
                # routable (UNKNOWN = the model file exists but the engine
                # has never proven it serves the model — a GGUF file on
                # disk is not proof of llama.cpp availability)
                continue
            # TEXT and TOOL_USE are execution prerequisites. Specialist labels
            # such as CODING/REASONING/TESTING are measured capabilities, not
            # hardcoded role claims: benchmark evidence ranks them below.
            # This keeps an unlabelled but healthy model eligible for discovery
            # instead of making one config entry the only coding candidate.
            core_caps = {CapabilityTag.TEXT, CapabilityTag.TOOL_USE}
            required_core = set(requirements.capabilities) & core_caps
            if not required_core <= set(record.capabilities):
                continue
            if evidence_required:
                measured_for_record = {
                    capability
                    for capability in evidence_required
                    if registry.capability_bundle(
                        record.model_id, record.engine, capability
                    ).samples
                    >= self._router.minimum_sample_count
                }
                if not evidence_required <= measured_for_record:
                    continue
            # Cold start: with no measured evidence for a specialist
            # capability anywhere in the registry, every healthy core-capable
            # model stays eligible for deterministic discovery. Labels alone
            # can never qualify — and never disqualify — a candidate.
            if requirements.needs_large_context and record.context_window < 16384:
                continue
            if (
                self._router.max_vram_bytes is not None
                and record.vram_bytes is not None
                and record.vram_bytes > self._router.max_vram_bytes
            ):
                continue
            if (
                self._router.max_ram_bytes is not None
                and record.ram_bytes is not None
                and record.ram_bytes > self._router.max_ram_bytes
            ):
                continue
            if (
                self._router.max_context_bytes is not None
                and record.context_window > self._router.max_context_bytes
            ):
                continue
            # Phase 14C-A: routing consumes the NEWEST recorded benchmark
            # version for this identity explicitly — a bench-v1 record never
            # influences a bench-v2 routing bundle (no version blending)
            versions = registry.metrics_versions(record.model_id, record.engine, task_class)
            bundle = registry.bundle_for(
                record.model_id,
                record.engine,
                task_class,
                benchmark_version=versions[-1] if versions else None,
            )
            # Specialist routing uses the explicit capability bundle as its
            # quality/latency evidence, rather than a role-derived task proxy.
            if specialist_caps and evidence_required:
                specialist_bundles = [
                    registry.capability_bundle(
                        record.model_id,
                        record.engine,
                        capability,
                        benchmark_version=versions[-1] if versions else None,
                    )
                    for capability in sorted(evidence_required)
                ]
                if specialist_bundles:
                    bundle = specialist_bundles[0]
            qualified.append((record, bundle))
        return task_class, qualified

    def rank(self, registry: ModelRegistry, requirements: TaskRequirements) -> list[CandidateScore]:
        task_class, qualified = self._hard_filter(registry, requirements)
        min_quality = (
            _quality_gate(self._router, requirements.latency)
            if requirements.verification_required
            else 0.0
        )
        ranked: list[CandidateScore] = []
        for record, bundle in qualified:
            measured = bundle.samples > 0 and bundle.success_rate is not None
            success = bundle.success_rate if measured else None
            # Roles are descriptive legacy metadata only. Routing quality is
            # based on measured outcomes; unmeasured candidates receive no
            # invented capability score and remain eligible for cold start.
            measured_quality = success if success is not None else 0.5
            speed_bonus = 0.0
            if bundle.median_tokens_per_second is not None:
                speed_bonus = min(
                    0.25,
                    bundle.median_tokens_per_second
                    / max(self._router.throughput_ref_tps, 1.0)
                    * 0.1,
                )
            # A measured success rate is already the evidence score; keep it
            # exact for compatibility and auditability. The throughput signal
            # is only a secondary bonus, while cold start remains neutral.
            score = measured_quality + speed_bonus if measured else 0.5
            # Phase 14C-B: the quality gate may reject a measured candidate
            # only when the evidence is SUFFICIENT (samples >=
            # minimum_sample_count); fewer samples stay in the ranking -
            # a single failed sample can no longer exclude a model, and no
            # success rate is ever invented or smoothed
            if (
                bundle.samples >= self._router.minimum_sample_count
                and success is not None
                and success < min_quality
            ):
                continue  # quality gate on sufficient measurements
            if success is None and min_quality > 1.0:
                continue
            ranked.append(
                CandidateScore(
                    model_id=record.model_id,
                    score=round(score, 4),
                    size_b=_size_proxy(record),
                    measured=measured,
                    reason=(
                        f"measured_quality={measured_quality:.2f} "
                        f"success={success} tps={bundle.median_tokens_per_second}"
                    ),
                    engine=record.engine,
                    median_latency_s=bundle.median_latency_s,
                    p95_latency_s=bundle.p95_latency_s,
                )
            )
        # unmeasured candidates only serve cold start; measured ones outrank
        # them. Deterministic precedence (Phase 14C-E): measured-first, then
        # size (smallest capable), then score, then MEASURED latency as the
        # tiebreak among otherwise-equivalent candidates (median, then p95;
        # unknown latency sorts last — never overrides capability, health,
        # quality, version, or engine isolation), then model_id.
        ranked.sort(
            key=lambda c: (
                not c.measured,
                c.size_b,
                -c.score,
                c.median_latency_s if c.median_latency_s is not None else float("inf"),
                c.p95_latency_s if c.p95_latency_s is not None else float("inf"),
                c.model_id,
            )
        )
        return ranked

    def _rank_quality(
        self, registry: ModelRegistry, requirements: TaskRequirements
    ) -> list[RankedCandidate]:
        """Quality-constrained candidate chain (SPEC §14D).

        Same hard filters as ``rank()``, then a conservative verified-success
        estimate (Wilson LCB) per candidate, then a reliability floor per task
        bucket, then a utility score over latency and memory. The chain is
        ordered highest utility first: reliability is enforced BEFORE
        efficiency, so a fast model never outranks a slower one that is
        measurably more reliable.
        """
        task_class, qualified = self._hard_filter(registry, requirements)
        bucket = task_bucket(requirements)
        floor = self._router.quality_min.get(bucket, 0.0)
        eta = self._router.utility_weights.get("eta", 1.0)
        alpha = self._router.utility_weights.get("alpha", 1.0)
        beta = self._router.utility_weights.get("beta", 0.5)
        candidates: list[RankedCandidate] = []
        for record, bundle in qualified:
            rel = reliability(bundle, prior=self._router.unmeasured_p_lcb)
            # The floor is a benchmark-derived policy: it only bites once
            # enough observations exist. Cold start is never gated.
            if (
                floor > 0.0
                and rel.measured
                and rel.n >= self._router.quality_min_samples
                and rel.p_lcb < floor
            ):
                continue
            memory_mib = estimate_memory_mib(record)
            score = utility(
                rel.p_lcb,
                bundle.p95_latency_s * 1000.0 if bundle.p95_latency_s is not None else None,
                memory_mib,
                tokens_per_second=bundle.median_tokens_per_second,
                throughput_ref_tps=self._router.throughput_ref_tps,
                eta=eta,
                alpha=alpha,
                beta=beta,
                delta=self._router.utility_weights.get("delta", 0.35),
                latency_ref_ms=self._router.latency_ref_ms,
                memory_ref_mib=self._router.memory_ref_mib,
            )
            candidates.append(
                RankedCandidate(
                    model_id=record.model_id,
                    engine=record.engine,
                    stage="qualified",
                    utility=round(score, 6),
                    p_lcb=round(rel.p_lcb, 4),
                    size_b=_size_proxy(record),
                    measured=rel.measured,
                    median_latency_s=bundle.median_latency_s,
                    p95_latency_s=bundle.p95_latency_s,
                    memory_mib=memory_mib,
                    reason=f"p_lcb={rel.p_lcb:.3f} n={rel.n} mem={memory_mib}",
                )
            )
        # efficiency-first among the reliability-qualified set; measured
        # evidence outranks unmeasured cold start (a conservative prior is
        # never presented as measured reliability)
        candidates.sort(
            key=lambda c: (
                not c.measured,
                -c.utility,
                c.size_b,
                c.model_id,
            )
        )
        return candidates

    def select(self, registry: ModelRegistry, requirements: TaskRequirements) -> SelectionResult:
        ranked = self.rank(registry, requirements)
        measured_qualified = [c for c in ranked if c.measured]
        pool = measured_qualified or ranked
        if not pool:
            raise ResourceUnavailable(
                "no model satisfies task capabilities/health (SPEC §13)",
                task_class=requirements.task_type.value,
            )
        # Phase 14D: the quality-constrained chain is the primary path. When
        # the bucket has benchmark evidence it decides the winner; without it
        # the deterministic size-based policy (the previous behavior) still
        # applies, so cold start is never broken.
        quality = self._rank_quality(registry, requirements)
        winner = (
            registry.get(quality[0].model_id, quality[0].engine or None)
            if quality
            else registry.get(pool[0].model_id, pool[0].engine or None)
        )
        return SelectionResult(
            selected=winner,
            requirements_key=requirements.task_type.value,
            ranked=ranked,
            quality_candidates=quality,
        )

    def select_critic(self, registry: ModelRegistry) -> ModelRecord:
        """Strongest specialist for review — never the default worker route.
        Phase 14C-H: measurement-aware — critics with SUFFICIENT measured
        evidence (samples >= minimum_sample_count, engine-exact) are
        preferred by measured quality, then size. Without measurements the
        existing size-based fallback applies deterministically. Critic
        metrics never influence the worker selection path."""
        # Review capability is measured, not granted by a role label. Every
        # healthy enabled model is eligible; measured evidence wins, while
        # cold-start falls back deterministically to the largest candidate.
        critics = [r for r in registry.enabled() if r.health is not ModelHealth.UNHEALTHY]
        if not critics:
            raise ResourceUnavailable("no critic-capable model registered")
        scored: list[tuple[bool, float | None, ModelRecord]] = []
        for r in critics:
            m = registry.metrics(r.model_id, r.engine)
            samples = sum(b.samples for b in m.benchmark.values()) + sum(
                p.samples for p in m.production.values()
            )
            rates = [b.success_rate for b in m.benchmark.values()] + [
                p.success_rate for p in m.production.values()
            ]
            known = [x for x in rates if x is not None]
            quality = max(known) if known else None
            sufficient = samples >= self._router.minimum_sample_count and quality is not None
            scored.append((sufficient, quality, r))
        if any(sufficient for sufficient, _q, _r in scored):
            measured = [s for s in scored if s[0]]
            measured.sort(key=lambda s: (-(s[1] or 0.0), -_size_proxy(s[2]), s[2].model_id))
            return measured[0][2]
        critics.sort(key=lambda r: (-_size_proxy(r), r.model_id))  # strongest plausible first
        return critics[0]


def caps_for(task_type: TaskType) -> list[CapabilityTag]:
    if task_type in (TaskType.CODING, TaskType.TESTING):
        return [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
    if task_type in (TaskType.SIMPLE_FILE, TaskType.TERMINAL):
        return [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
    return [CapabilityTag.TEXT]
