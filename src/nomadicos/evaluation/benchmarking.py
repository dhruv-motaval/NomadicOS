"""Benchmark runner (SPEC §52A): standardized tasks, fairness, raw records.

The runner measures; it never authorizes and never invents numbers. Full
production-path coding tasks are executed by injected callbacks (wired to
the orchestration brick in SPEC §53 Phase 14).
"""

from __future__ import annotations

import platform
import time
from typing import Any, Protocol

from pydantic import Field

from nomadicos.contracts.benchmark import BenchmarkRecord, BenchmarkTask
from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import ModelRecord
from nomadicos.inference.base import ChatMessage, GenerationRequest, InferenceEngine
from nomadicos.kernel.errors import ModelError
from nomadicos.kernel.ids import new_id
from nomadicos.registry.model_registry import ModelRegistry


class ProbeCase(Contract):
    """Fast correctness probe (cheap measurement of model behavior)."""

    id: str
    prompt: str
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    category: str = "bug_fix"


class TaskRunOutcome(Contract):
    success: bool
    goal_verified: bool | None = None
    tests_passed: bool | None = None
    repair_success: bool | None = None
    tool_success: bool | None = None
    verification_compliance: bool | None = None
    steps: int | None = None
    retries: int | None = None
    failure_type: str | None = None
    total_latency_s: float | None = None
    time_to_first_token_ms: float | None = None
    tokens_generated: int | None = None
    tokens_per_second: float | None = None
    resource_usage: dict[str, float] = Field(default_factory=dict)


class BenchmarkExecution(Protocol):
    """How an orchestrated run reports its outcome for a model."""

    def __call__(self, task: BenchmarkTask, model: ModelRecord) -> TaskRunOutcome: ...


def environment_signature(extra: dict[str, Any] | None = None) -> dict[str, str]:
    """Deterministic fairness context recorded with every measurement."""
    sig = {"platform": platform.system(), "python": platform.python_version()}
    if extra:
        sig.update({k: str(v) for k, v in extra.items()})
    return sig


class BenchmarkRunner:
    def __init__(
        self, registry: ModelRegistry, engine: InferenceEngine, benchmark_version: str
    ) -> None:
        self._registry = registry
        self._engine = engine
        self._version = benchmark_version

    async def probe(self, model: ModelRecord, case: ProbeCase) -> BenchmarkRecord:
        """One measured probe; failures are recorded honestly, never skipped."""
        request = GenerationRequest(
            model_id=model.model_id, messages=[ChatMessage(role="user", content=case.prompt)]
        )
        started = time.monotonic()
        try:
            response = await self._engine.generate(request)
        except ModelError as exc:
            return self._store(
                model,
                case,
                success=False,
                failure_type="MODEL_ERROR",
                latency=time.monotonic() - started,
                note=str(exc),
            )
        except Exception as exc:  # engine down: record, do not fabricate
            return self._store(
                model,
                case,
                success=False,
                failure_type=type(exc).__name__,
                latency=time.monotonic() - started,
            )
        latency = time.monotonic() - started
        success = all(needle in response.text for needle in case.must_contain) and not any(
            needle in response.text for needle in case.must_not_contain
        )
        return self._store(
            model,
            case,
            success=success,
            failure_type=None if success else "WRONG_OUTPUT",
            latency=latency,
            tokens=response.output_tokens,
            tps=response.tokens_per_second,
            ttft=response.time_to_first_token_s,
        )

    def run_workspace_task(
        self, model: ModelRecord, task: BenchmarkTask, outcome: TaskRunOutcome
    ) -> BenchmarkRecord:
        """Record an outcome produced by a real production-path run (§39)."""
        record = BenchmarkRecord(
            id=new_id("bench"),
            model_id=model.model_id,
            engine_id=model.engine,
            benchmark_version=self._version,
            task_id=task.id,
            task_category=task.category,
            language=task.language,
            framework=task.framework,
            difficulty=task.difficulty,
            success=outcome.success,
            goal_verified=outcome.goal_verified,
            tests_passed=outcome.tests_passed,
            repair_success=outcome.repair_success,
            tool_success=outcome.tool_success,
            verification_compliance=outcome.verification_compliance,
            time_to_first_token_ms=outcome.time_to_first_token_ms,
            total_latency_ms=outcome.total_latency_s * 1000.0 if outcome.total_latency_s else None,
            tokens_generated=outcome.tokens_generated,
            tokens_per_second=outcome.tokens_per_second,
            steps=outcome.steps,
            retries=outcome.retries,
            failure_type=outcome.failure_type,
            resource_usage=outcome.resource_usage,
            environment=environment_signature(),
        )
        self._registry.record_benchmark(record)
        return record

    def comparison(self, model_ids: list[str], engine: str) -> dict[str, Any]:
        """Per-model class breakdown for a fair same-benchmark comparison
        (engine-scoped, Phase 14A.1)."""
        return {mid: self._registry.metrics(mid, engine).benchmark for mid in model_ids}

    # ------------------------------------------------------------- helper --
    def _store(
        self,
        model: ModelRecord,
        case: ProbeCase,
        *,
        success: bool,
        failure_type: str | None,
        latency: float,
        tokens: int | None = None,
        tps: float | None = None,
        ttft: float | None = None,
        note: str = "",
    ) -> BenchmarkRecord:
        record = BenchmarkRecord(
            id=new_id("bench"),
            model_id=model.model_id,
            engine_id=model.engine,
            benchmark_version=self._version,
            task_id=case.id,
            task_category=case.category,  # type: ignore[arg-type]
            language="_probe",
            difficulty=0.1,
            success=success,
            total_latency_ms=round(latency * 1000.0, 2),
            time_to_first_token_ms=ttft * 1000.0 if ttft is not None else None,
            tokens_generated=tokens,
            tokens_per_second=tps,
            failure_type=failure_type,
            environment=environment_signature({"note": note} if note else None),
        )
        self._registry.record_benchmark(record)
        return record
