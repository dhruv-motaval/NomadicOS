"""Quality-constrained routing math (Phase 14D).

Hard constraints first, then a confidence-aware reliability estimate, then a
utility score over the qualified set. Nothing here invents measurements or
decides verification: it only ranks candidates for the orchestrator to try.
"""

from __future__ import annotations

from dataclasses import dataclass

from nomadicos.contracts.model import (
    CapabilityTag,
    MetricsBundle,
    ModelRecord,
    TaskRequirements,
)

#: z for a two-sided 95% confidence interval (Wilson lower bound)
_Z_95 = 1.96


@dataclass(frozen=True, slots=True)
class ReliabilityEstimate:
    """Conservative verified-success probability for one (model, bucket)."""

    p_lcb: float
    n: int
    s: int
    p_hat: float | None
    measured: bool

    @property
    def is_known(self) -> bool:
        return self.measured and self.n > 0


def wilson_lower_bound(s: int, n: int, z: float = _Z_95) -> float:
    """Wilson score lower confidence bound for a binomial proportion.

    A model with two successful attempts is NOT therefore perfect: the bound
    stays well below 1.0 until the sample is large enough to support it. When
    there is no evidence the caller must use a conservative prior rather than
    pretending reliability is known.
    """
    if n <= 0:
        return 0.0
    p_hat = s / n
    denom = 1.0 + z * z / n
    centre = p_hat + z * z / (2.0 * n)
    spread = z * ((p_hat * (1.0 - p_hat) / n) + (z * z / (4.0 * n * n))) ** 0.5
    return max(0.0, (centre - spread) / denom)


def reliability(bundle: MetricsBundle, *, prior: float = 0.5) -> ReliabilityEstimate:
    """Estimate verified success from a measured bundle, or fall back to a
    conservative prior when nothing has been observed."""
    n = int(bundle.samples or 0)
    s = int(round((bundle.success_rate or 0.0) * n))
    if n > 0 and bundle.success_rate is not None:
        return ReliabilityEstimate(
            p_lcb=round(wilson_lower_bound(s, n), 4),
            n=n,
            s=s,
            p_hat=round(bundle.success_rate, 4),
            measured=True,
        )
    return ReliabilityEstimate(
        p_lcb=round(prior, 4),
        n=0,
        s=0,
        p_hat=None,
        measured=False,
    )


def utility(
    p_lcb: float,
    latency_ms: float | None,
    memory_mib: float | None,
    *,
    eta: float = 1.0,
    alpha: float = 1.0,
    beta: float = 0.5,
    gamma: float = 0.0,
    cost_per_task: float | None = None,
    latency_ref_ms: float = 30_000.0,
    memory_ref_mib: float = 8_192.0,
    cost_ref: float = 1.0,
    epsilon: float = 1e-6,
) -> float:
    """Quality-constrained utility (SPEC §14D).

    Reliability first (raised to eta), then penalised by latency, memory and
    cost. Local-first configs drop cost (gamma = 0). Unmeasured latency/memory
    are treated as neutral (ratio 1) rather than penalised.
    """
    if p_lcb <= 0.0:
        return 0.0
    latency_ratio = (latency_ms / latency_ref_ms) if latency_ms else 1.0
    memory_ratio = (memory_mib / memory_ref_mib) if memory_mib else 1.0
    cost_ratio = ((cost_per_task or 0.0) + epsilon) / cost_ref if gamma > 0 else 1.0
    return (p_lcb**eta) / ((latency_ratio**alpha) * (memory_ratio**beta) * (cost_ratio**gamma))


def estimate_memory_mib(record: ModelRecord) -> float | None:
    """A serving memory estimate from KNOWN declared fields only.

    Never inferred from size_hint_b (a ranking proxy, not usage). vram_bytes
    is the stronger signal; ram_bytes is the fallback. Either unknown => None
    => the utility term is neutral rather than invented.
    """
    if record.vram_bytes:
        return record.vram_bytes / (1024.0 * 1024.0)
    if record.ram_bytes:
        return record.ram_bytes / (1024.0 * 1024.0)
    return None


def task_bucket(requirements: TaskRequirements) -> str:
    """Task bucket for separate performance profiles (§14D-2).

    Splitting data into too many tiny categories makes success-rate estimates
    unreliable, so the buckets are coarse and only split further when enough
    observations exist. Coding tasks that also carry reasoning intent get their
    own bucket because correctness under constraints is a different failure
    mode than straightforward implementation.
    """
    base = requirements.task_type.value
    if (
        requirements.task_type.value == "coding"
        and CapabilityTag.REASONING in requirements.capabilities
    ):
        return "coding+reasoning"
    return base
