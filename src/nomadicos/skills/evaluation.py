"""Skill evaluation (Sprint 8): measured quality feeding retrieval.

    skill candidate → evaluation → measured quality → ranking evidence

Deterministic and honest: evaluation AGGREGATES recorded outcomes
(SkillResult values produced by real runs through the existing runtime)
into the skill definition's evidence — it never invents a metric, never
executes by itself, and never bypasses the runtime. An optional bounded
case-runner lets hosts evaluate deterministic cases through an injected
callback (which itself must go through the existing pipeline).

Integration with Sprint 7: the evidence fields are the same measured-
evidence pattern as the model registry (samples == 0 means NOTHING is
known); ranking consumes ``success_rate`` only when ``samples > 0``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nomadicos.contracts.skills import (
    SkillDefinition,
    SkillEvaluationRecord,
    SkillResult,
)

MAX_EVALUATION_CASES = 32


def record_outcome(
    definition: SkillDefinition, result: SkillResult
) -> SkillDefinition:
    """Append one real outcome to the skill's measured evidence.

    Deterministic recomputation; the definition is immutable so a NEW
    definition version-of-record is returned (registry.set-style update
    is the caller's choice). Validation failures are counted by status
    mapping: a FAILED/ERRORED execution is a failure; SUCCEEDED counts
    as success."""
    successes = definition.evidence.successes + (1 if result.status.value == "SUCCEEDED" else 0)
    failures = definition.evidence.failures + (0 if result.status.value == "SUCCEEDED" else 0)
    samples = definition.evidence.samples + 1
    return definition.model_copy(
        update={
            "evidence": definition.evidence.model_copy(
                update={
                    "samples": samples,
                    "successes": successes,
                    "failures": failures,
                    "success_rate": round(successes / samples, 4),
                }
            )
        }
    )


def evaluate(
    definition: SkillDefinition,
    run: Callable[[SkillDefinition], SkillResult],
    cases: int = 3,
) -> SkillEvaluationRecord:
    """Bounded deterministic evaluation through an injected runner.

    The callback owns execution (it must route through the existing
    validator/authorization/executor pipeline); this function only
    aggregates honestly. Fail-safe: a callback exception counts as a
    FAILED outcome — never skipped, never invented."""
    cases = min(max(cases, 0), MAX_EVALUATION_CASES)
    successes = 0
    failures = 0
    validation_failures = 0
    for _index in range(cases):
        try:
            outcome = run(definition)
        except Exception:  # noqa: BLE001 - honest failure, never skipped
            failures += 1
            continue
        if outcome.status.value == "SUCCEEDED":
            successes += 1
        elif outcome.failure_category == "VALIDATION":
            validation_failures += 1
        else:
            failures += 1
    total = successes + failures + validation_failures
    return SkillEvaluationRecord(
        skill_id=definition.skill_id,
        version=definition.version,
        cases=total,
        successes=successes,
        failures=failures,
        validation_failures=validation_failures,
        success_rate=round(successes / total, 4) if total else None,
        benchmark_version=f"skill-eval-{datetime.now(UTC).date().isoformat()}",
    )
