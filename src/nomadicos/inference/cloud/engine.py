"""CloudEngine: the ``InferenceEngine`` for owner-configured cloud providers
(SPEC §9-10).

One engine fronts every configured provider; models are addressed as
``<provider>/<provider model id>``. The Brain and workers never see providers,
URLs, or credentials: they ask the router for a model and call the engine
through the normal ``InferenceEngine`` contract. Cloud output is UNTRUSTED
text — it goes through the same Action IR -> authority -> executor -> verifier
pipeline as local output, and this engine never hands a model any tool,
filesystem, database or authority access.

Per request the engine enforces, in order:
  admission (owner allowlist, ``free_only`` pricing/freshness, capability,
  context) -> bounded retries with backoff -> bounded fallback to OTHER
  admitted models (never to a model the policy would not admit as primary) ->
  schema validation of structured output -> a recorded ``InferenceTrace``.

Retry policy (all inside ``max_retries`` / ``max_fallbacks`` / ``total_timeout_s``):
  * rate limit / server error: back off and retry the same model;
  * timeout / empty or malformed reply: move to the next model if there is
    one, otherwise retry the only model we have;
  * schema violation: the model is alive, so re-ask it WITH the validation
    error (a correction, not a repeat);
  * a model that failed on availability cools down; when every admitted model
    is cooling down the request waits for the first one instead of failing.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from nomadicos.contracts.model import CloudModelMeta, ModelHealth
from nomadicos.inference.base import (
    AttemptRecord,
    ChatMessage,
    Cost,
    EngineHealth,
    GenerationRequest,
    GenerationResponse,
    InferenceEngine,
    InferenceTrace,
)
from nomadicos.inference.cloud.policy import (
    Admission,
    admit,
    declared_pricing,
    estimate_tokens,
    qualified_id,
)
from nomadicos.inference.cloud.providers import OpenAICompatProvider, ProviderReply
from nomadicos.inference.cloud.schema import SchemaError, validate_text
from nomadicos.kernel.config import CloudConfig
from nomadicos.kernel.errors import (
    CloudInferenceError,
    ContextOverflow,
    FreeOnlyExcluded,
    InferenceTimeout,
    MalformedResponse,
    ModelUnavailable,
    RateLimited,
    RetryBudgetExhausted,
    SchemaValidationFailed,
)
from nomadicos.kernel.ids import new_id

TraceSink = Callable[[InferenceTrace], None]

#: after these, another model is a better bet than repeating the same one
_PREFER_FALLBACK = (InferenceTimeout, MalformedResponse)
#: AVAILABILITY failures cool a model down; content failures (schema) do not
_COOLDOWN = (ModelUnavailable, InferenceTimeout, MalformedResponse)
#: how much of a rejected reply is shown back to the model in a repair retry
_REPAIR_CHARS = 4000


def _repair(request: GenerationRequest, rejected: str, error: str) -> GenerationRequest:
    """The ORIGINAL request plus the rejected reply and why it was rejected,
    so a same-model retry is a correction rather than a repeat."""
    return request.model_copy(
        update={
            "messages": [
                *request.messages,
                ChatMessage(role="assistant", content=rejected[:_REPAIR_CHARS]),
                ChatMessage(
                    role="user",
                    content=(
                        f"That reply was rejected: {error}. Reply again with ONLY one JSON "
                        "value that satisfies the required schema. No prose, no code fences."
                    ),
                ),
            ]
        }
    )


def _cost(reply: ProviderReply, meta: CloudModelMeta) -> Cost:
    if reply.reported_cost_usd is not None:
        return Cost(usd=reply.reported_cost_usd, basis="provider_reported")
    p = meta.pricing
    if p is None or reply.input_tokens is None or reply.output_tokens is None:
        return Cost()
    try:
        usd = (
            Decimal(p.prompt or "x") * reply.input_tokens
            + Decimal(p.completion or "x") * reply.output_tokens
        )
        if p.request is not None:
            usd += Decimal(p.request)
    except InvalidOperation:
        return Cost()
    return Cost(usd=float(usd), basis="estimated_from_catalog")


class CloudEngine(InferenceEngine):
    engine_id = "cloud"

    def __init__(
        self,
        cfg: CloudConfig,
        providers: dict[str, OpenAICompatProvider],
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.cfg = cfg
        self.providers = providers
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mono = monotonic
        self._sleep = sleep
        self._trace_sink = trace_sink
        #: provider-qualified id -> latest normalized catalog entry
        self.catalog: dict[str, CloudModelMeta] = {}
        #: provider -> "ok" | typed error kind | "never"
        self.catalog_status: dict[str, str] = dict.fromkeys(providers, "never")
        self.traces: deque[InferenceTrace] = deque(maxlen=256)
        #: provider requests in flight, shared by every concurrent mission
        self._slots = asyncio.Semaphore(cfg.max_concurrent_requests)
        #: qualified id -> monotonic time until which it is skipped
        self._cooldown: dict[str, float] = {}

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()

    # ------------------------------------------------------------- catalog --
    async def refresh_catalog(self) -> dict[str, str]:
        """Re-read every provider's model catalog (a metadata GET: no prompt
        is ever sent, so it cannot be billed as inference). A provider that
        fails keeps its previous entries, which then age out under
        ``max_catalog_age_s`` — stale pricing is excluded, not trusted."""
        now = self._clock()
        for name, provider in self.providers.items():
            try:
                metas = await provider.list_catalog(now, self.cfg.catalog_timeout_s)
            except CloudInferenceError as exc:
                self.catalog_status[name] = exc.kind
                continue
            self.catalog = {k: v for k, v in self.catalog.items() if v.provider != name}
            for meta in metas:
                self.catalog[qualified_id(name, meta.provider_model_id)] = meta
            self.catalog_status[name] = "ok"
        return dict(self.catalog_status)

    async def list_models(self) -> list[str]:
        return sorted(self.catalog)

    async def health(self) -> EngineHealth:
        """From the last catalog refresh only — never a probe prompt."""
        ok = [n for n, s in self.catalog_status.items() if s == "ok"]
        if ok:
            status = ModelHealth.HEALTHY if len(ok) == len(self.providers) else ModelHealth.DEGRADED
        elif all(s == "never" for s in self.catalog_status.values()):
            status = ModelHealth.UNKNOWN
        else:
            status = ModelHealth.UNHEALTHY
        detail = ", ".join(f"{n}={s}" for n, s in sorted(self.catalog_status.items()))
        return EngineHealth(status=status, detail=detail, models=sorted(self.catalog))

    # ------------------------------------------------------------ planning --
    def _admit(
        self, meta: CloudModelMeta, request: GenerationRequest, need: int, now: datetime
    ) -> Admission:
        return admit(
            meta,
            self.cfg,
            now=now,
            structured=request.response_schema is not None,
            images=bool(request.images),
            min_context=need,
        )

    def _cooling(self, qid: str) -> float:
        """Seconds this model must still be skipped (0.0 = usable now)."""
        left = self._cooldown.get(qid, 0.0) - self._mono()
        return left if left > 1e-3 else 0.0

    def _cooldown_wait(self, request: GenerationRequest, need: int, now: datetime) -> float | None:
        """Seconds until the first model that is admitted for this request but
        cooling down becomes usable; None when no such model exists."""
        waits = [
            left
            for qid, meta in self.catalog.items()
            if (left := self._cooling(qid)) and self._admit(meta, request, need, now).ok
        ]
        return min(waits) if waits else None

    def _chain(
        self,
        request: GenerationRequest,
        need: int,
        now: datetime,
        reasons: list[str],
    ) -> list[tuple[str, CloudModelMeta]]:
        """Primary (if admitted) then up to ``max_fallbacks`` other admitted
        models. Every member passed ``admit`` with this request's needs, so
        fallback can never reach a model the policy would reject as primary
        (in particular: no paid model while ``free_only`` is on)."""

        def ok(qid: str, meta: CloudModelMeta) -> tuple[bool, str]:
            if self._cooling(qid):
                return False, "cooling down after a recent failure"
            a = self._admit(meta, request, need, now)
            return a.ok, a.reason

        chain: list[tuple[str, CloudModelMeta]] = []
        primary = self.catalog.get(request.model_id)
        if primary is None:
            reasons.append(f"{request.model_id}: not in catalog")
        else:
            good, why = ok(request.model_id, primary)
            if good:
                chain.append((request.model_id, primary))
            else:
                reasons.append(f"{request.model_id}: {why}")
        want_provider = request.model_id.partition("/")[0]
        others = sorted(
            (
                (qid, m)
                for qid, m in self.catalog.items()
                if qid != request.model_id and ok(qid, m)[0]
            ),
            key=lambda item: (
                item[1].provider != want_provider,
                -(item[1].context_window or 0),
                item[0],
            ),
        )
        chain.extend(others[: self.cfg.max_fallbacks + (0 if chain else 1)])
        return chain

    # ---------------------------------------------------------- generation --
    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        started = self._mono()
        deadline = started + self.cfg.total_timeout_s
        now = self._clock()
        structured = request.response_schema is not None
        est_in = estimate_tokens(sum(len(m.content) for m in request.messages), len(request.images))
        need = est_in + (request.max_tokens or self.cfg.default_max_tokens)
        trace_id = new_id("trace")
        reasons: list[str] = []
        attempts: list[AttemptRecord] = []
        errors: list[CloudInferenceError] = []
        retries = fallbacks = 0

        def finish(
            outcome: str,
            *,
            meta: CloudModelMeta | None = None,
            qid: str | None = None,
            reply: ProviderReply | None = None,
            validation: str = "not_requested",
            error_kind: str | None = None,
        ) -> InferenceTrace:
            trace = InferenceTrace(
                trace_id=trace_id,
                outcome="success" if outcome == "success" else "failure",
                requested_model_id=request.model_id,
                model_id=qid,
                provider=meta.provider if meta else None,
                served_model=reply.served_model if reply else None,
                latency_s=round(self._mono() - started, 6),
                attempts=attempts,
                retries=retries,
                fallbacks=fallbacks,
                fallback_reasons=reasons,
                validation=validation,  # type: ignore[arg-type]
                finish_reason=reply.finish_reason if reply else None,
                input_tokens=reply.input_tokens if reply else None,
                output_tokens=reply.output_tokens if reply else None,
                cost=_cost(reply, meta) if reply and meta else Cost(),
                free_only=self.cfg.free_only,
                catalog_retrieved_at=meta.catalog_retrieved_at if meta else None,
                declared_pricing=declared_pricing(meta) if meta else {},
                context_window=meta.context_window if meta else None,
                estimated_input_tokens=est_in,
                error_kind=error_kind,
            )
            self.traces.append(trace)
            if self._trace_sink is not None:
                try:
                    self._trace_sink(trace)
                except Exception:  # observability must never break inference
                    pass
            return trace

        chain = self._chain(request, need, now, reasons)
        if not chain:
            wait = self._cooldown_wait(request, need, now)
            if wait is not None and wait < deadline - self._mono():
                # backpressure, not failure: slow down instead of burning the
                # caller's recovery budget on instant errors
                reasons.append(f"waited {wait:.0f}s: every admitted model was cooling down")
                await self._sleep(wait)
                now = self._clock()
                chain = self._chain(request, need, now, reasons)
        if not chain:
            primary = self.catalog.get(request.model_id)
            if self._cooldown_wait(request, need, now) is not None:
                exc: CloudInferenceError = RateLimited(
                    "every admitted model is cooling down after recent failures",
                    retryable=True,
                )
            elif (
                primary is not None
                and admit(
                    primary, self.cfg, now=now, structured=structured, images=bool(request.images)
                ).ok
            ):  # only the context requirement excludes it
                exc = ContextOverflow(
                    f"request needs ~{need} tokens; no admitted model has that context",
                    needed_tokens=need,
                )
            elif self.cfg.free_only:
                exc = FreeOnlyExcluded("no owner-allowed free model satisfies this request")
            else:
                exc = ModelUnavailable(
                    "no owner-allowed model satisfies this request", retryable=False
                )
            exc.context["trace_id"] = finish("failure", error_kind=exc.kind).trace_id
            raise exc

        if chain[0][0] != request.model_id:
            fallbacks += 1  # substituted before any call; reason already recorded
        timed_out = False
        for pos, (qid, meta) in enumerate(chain):
            if pos > 0 and errors:
                fallbacks += 1
                reasons.append(f"{chain[pos - 1][0]}: {errors[-1].kind}")
            provider = self.providers[meta.provider]
            last_candidate = pos == len(chain) - 1
            current = request  # a schema repair swaps in a corrected request for THIS model
            for attempt in range(self.cfg.max_retries + 1):
                remaining = deadline - self._mono()
                if remaining <= 0:
                    timed_out = True
                    break
                a0 = self._mono()
                rejected = ""
                try:
                    async with self._slots:
                        reply = await provider.chat(
                            current,
                            meta.provider_model_id,
                            # the owner's cloud timeout is a cap a caller cannot raise
                            timeout_s=min(
                                request.timeout_s or remaining,
                                self.cfg.request_timeout_s,
                                remaining,
                            ),
                            free_only=self.cfg.free_only,
                        )
                    if structured:
                        assert request.response_schema is not None
                        try:
                            validate_text(reply.text, request.response_schema)
                        except SchemaError as exc_s:
                            rejected = reply.text
                            raise SchemaValidationFailed(
                                f"output violates requested schema "
                                f"(finish_reason={reply.finish_reason}): {exc_s}"[:200],
                                retryable=True,
                            ) from exc_s
                except CloudInferenceError as exc:
                    attempts.append(
                        AttemptRecord(
                            model_id=qid,
                            outcome=exc.kind,
                            latency_s=round(self._mono() - a0, 6),
                            detail=exc.message[:200],
                        )
                    )
                    errors.append(exc)
                    if isinstance(exc, _COOLDOWN) and self.cfg.rate_limit_cooldown_s:
                        self._cooldown[qid] = self._mono() + self.cfg.rate_limit_cooldown_s
                    if not exc.retryable or attempt == self.cfg.max_retries:
                        break
                    if isinstance(exc, _PREFER_FALLBACK) and not last_candidate:
                        break
                    if isinstance(exc, SchemaValidationFailed):
                        grown = need + estimate_tokens(min(len(rejected), _REPAIR_CHARS) + 300)
                        if meta.context_window is not None and grown > meta.context_window:
                            break  # the correction would not fit this model's context
                        current = _repair(request, rejected, exc.message)
                        delay = 0.0  # the model is responsive: no need to wait
                    else:
                        delay = min(self.cfg.retry_backoff_s * (2**attempt), self.cfg.max_backoff_s)
                        retry_after = (
                            exc.context.get("retry_after_s")
                            if isinstance(exc, RateLimited)
                            else None
                        )
                        if isinstance(retry_after, float):
                            if retry_after > self.cfg.max_backoff_s and not last_candidate:
                                break  # another model beats a long wait
                            delay = retry_after
                    if delay >= deadline - self._mono():
                        break  # the wait does not fit this request's time budget
                    retries += 1
                    if delay:
                        await self._sleep(delay)
                    continue
                self._cooldown.pop(qid, None)  # it answered: usable again
                attempts.append(
                    AttemptRecord(model_id=qid, outcome="ok", latency_s=round(self._mono() - a0, 6))
                )
                trace = finish(
                    "success",
                    meta=meta,
                    qid=qid,
                    reply=reply,
                    validation="passed" if structured else "not_requested",
                )
                latency = max(trace.latency_s, 1e-6)
                return GenerationResponse(
                    engine_id=self.engine_id,
                    model_id=qid,
                    text=reply.text,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    latency_s=latency,
                    tokens_per_second=round(reply.output_tokens / latency, 2)
                    if reply.output_tokens
                    else None,
                    trace=trace,
                )
            if timed_out:
                break

        if errors and all(isinstance(e, ContextOverflow) for e in errors):
            final: CloudInferenceError = errors[-1]
        else:
            last = errors[-1].kind if errors else "timeout"
            final = RetryBudgetExhausted(
                f"retry/fallback budget exhausted after {len(attempts)} attempt(s); last={last}",
                attempts=len(attempts),
                last_kind=last,
                total_timeout=timed_out,
            )
            if errors:
                final.__cause__ = errors[-1]
        validation = (
            "failed"
            if any(isinstance(e, SchemaValidationFailed) for e in errors)
            else "not_requested"
        )
        final.context["trace_id"] = finish(
            "failure", validation=validation, error_kind=final.kind
        ).trace_id
        raise final

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationResponse]:
        """Cloud responses are validated whole before use, so streaming is a
        single delta carrying the validated text."""
        full = await self.generate(request)
        yield full.model_copy(update={"is_delta": True})
