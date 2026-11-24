"""Cloud provider adapters (SPEC §9-10): OpenAI-compatible chat + catalog.

``OpenAICompatProvider`` speaks the de-facto ``/chat/completions`` + ``/models``
surface, so an owner-configured custom provider needs only a URL and a
credential REFERENCE. ``OpenRouterProvider`` adds OpenRouter-only request
extras. Providers are transport + normalization: they never choose models,
enforce policy, or retry (the engine does), and they never expose the API
key in errors, traces, or prompts.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from typing import Any

import httpx

from nomadicos.contracts.model import CloudModelMeta, Pricing
from nomadicos.inference.base import GenerationRequest
from nomadicos.kernel.config import CloudProviderConfig, _load_env_file, _repo_root
from nomadicos.kernel.errors import (
    CloudInferenceError,
    ContextOverflow,
    InferenceTimeout,
    MalformedResponse,
    ModelUnavailable,
    RateLimited,
)

#: JSON-SCHEMA output needs ``structured_outputs``. ``response_format`` alone is
#: plain JSON mode: sending a schema to such a model is refused (live evidence:
#: OpenRouter 404 "No endpoints found that can handle the requested parameters").
_SCHEMA_PARAM = "structured_outputs"
_CONTEXT_HINT = re.compile(r"context|maximum.*tokens|too many tokens|too long", re.IGNORECASE)


@dataclass(frozen=True)
class ProviderReply:
    text: str
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reported_cost_usd: float | None
    served_model: str | None


async def _within[T](awaitable: Awaitable[T], seconds: float) -> T:
    """Wall-clock deadline that does not depend on the awaited code honouring
    cancellation. Live evidence: an httpx read under ``asyncio.timeout(300)``
    and a 900s ``wait_for`` was still running after 25 minutes; the cancel was
    absorbed below httpx while OpenRouter kept a slow reply trickling. Here
    the request runs as its own task and we stop WAITING at the deadline,
    whatever that task does with its cancel."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        task.cancel()  # our caller gave up: do not leave the request running
        raise
    if task in done:
        return task.result()
    task.cancel()
    # retrieve the abandoned task's outcome so it is never reported as unhandled
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    raise TimeoutError(f"no reply within {seconds:.0f}s")


def resolve_secret(name: str) -> str | None:
    """Credential REFERENCE -> value: process environment first, then the
    repository-root ``.env`` (never committed). Returns None when unset."""
    value = os.environ.get(name) or _load_env_file(_repo_root() / ".env").get(name)
    return value or None


def _price(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except InvalidOperation:
        return None


def _per_million(value: Any) -> str | None:
    price = _price(value)
    return None if price is None else str(Decimal(price) / 1_000_000)


def _pricing(raw: dict[str, Any]) -> Pricing:
    """OpenRouter declares USD per token (``prompt``/``completion``); gateways
    such as OmniRoute declare USD per MILLION tokens (``input``/``output``/
    ``reasoning``). Both normalize to per token; an undeclared field stays None."""

    def pick(per_token: str, per_million: str) -> str | None:
        if per_token in raw:
            return _price(raw.get(per_token))
        return _per_million(raw.get(per_million))

    return Pricing(
        prompt=pick("prompt", "input"),
        completion=pick("completion", "output"),
        request=_price(raw.get("request")),
        image=_price(raw.get("image")),
        internal_reasoning=pick("internal_reasoning", "reasoning"),
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        cfg: CloudProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        secret_resolver: Callable[[str], str | None] = resolve_secret,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(base_url=str(cfg.base_url))
        self._resolve = secret_resolver

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ plumbing --
    def _key(self) -> str:
        key = self._resolve(self.cfg.api_key_env)
        if not key:
            raise ModelUnavailable(
                f"credential {self.cfg.api_key_env!r} is not set for provider {self.name!r}",
                retryable=False,
            )
        return key

    @staticmethod
    def _scrub(text: str, key: str | None = None) -> str:
        if key:
            text = text.replace(key, "***")
        return re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)[:200]

    def _map_http(
        self, status: int, body_text: str, retry_after: str | None, key: str
    ) -> CloudInferenceError:
        detail = self._scrub(body_text, key)
        if status == 429:
            try:
                wait = float(retry_after) if retry_after else None
            except ValueError:
                wait = None
            # keep the provider's own (scrubbed) words: they say WHICH limit was hit.
            # A per-DAY quota (OpenRouter: "free-models-per-day") cannot clear by
            # waiting seconds, so retrying it only burns time: fail fast instead.
            daily = "per-day" in body_text.lower() or "per day" in body_text.lower()
            return RateLimited(
                f"rate limited by {self.name}: {detail}",
                retry_after_s=wait,
                retryable=not daily,
                daily_quota=daily,
            )
        if status in (408, 504):
            return InferenceTimeout(f"{self.name} gateway timeout ({status})")
        if status == 402:  # payment required: never retry, never assume free
            return ModelUnavailable(f"{self.name} requires payment (402)", retryable=False)
        if status in (401, 403, 404):
            return ModelUnavailable(f"{self.name} refused request ({status})", retryable=False)
        if status == 400 and _CONTEXT_HINT.search(body_text):
            return ContextOverflow(f"{self.name} rejected context size: {detail}")
        if status >= 500:
            return ModelUnavailable(f"{self.name} server error {status}", retryable=True)
        return ModelUnavailable(
            f"{self.name} rejected request ({status}): {detail}", retryable=False
        )

    # ------------------------------------------------------------- catalog --
    async def list_catalog(self, now: datetime, timeout_s: float) -> list[CloudModelMeta]:
        key = self._key()
        try:
            resp = await _within(  # wall clock, see _within()
                self._client.get(
                    "/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout_s
                ),
                timeout_s,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise InferenceTimeout(f"{self.name} catalog timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"{self.name} catalog unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise self._map_http(resp.status_code, resp.text, resp.headers.get("retry-after"), key)
        try:
            data = resp.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise MalformedResponse(f"{self.name} catalog is not a model list") from exc
        if not isinstance(data, list):
            raise MalformedResponse(f"{self.name} catalog is not a model list")
        return [m for m in (self.normalize(e, now) for e in data) if m is not None]

    def normalize(self, entry: Any, now: datetime) -> CloudModelMeta | None:
        """Provider catalog entry -> typed metadata. Missing fields stay
        None/empty (unknown), never defaulted to favourable values."""
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            return None
        raw_price = entry.get("pricing")
        pricing = _pricing(raw_price) if isinstance(raw_price, dict) else None
        if pricing is None and any(fnmatchcase(entry["id"], p) for p in self.cfg.free_models):
            pricing = Pricing(prompt="0", completion="0", declared_by="owner")
        raw_params = entry.get("supported_parameters")
        params = (
            [p for p in raw_params if isinstance(p, str)] if isinstance(raw_params, list) else []
        )
        top = _dict(entry.get("top_provider"))
        raw_modalities = _dict(entry.get("architecture")).get("input_modalities")
        modalities = (
            [m for m in raw_modalities if isinstance(m, str)]
            if isinstance(raw_modalities, list) and raw_modalities
            else ["text"]
        )
        return CloudModelMeta(
            provider=self.name,
            provider_model_id=entry["id"],
            pricing=pricing,
            supported_parameters=params,
            structured_output=_SCHEMA_PARAM in params or self.cfg.assume_structured_output,
            input_modalities=modalities,
            context_window=_int(entry.get("context_length"))
            or _int(top.get("context_length"))
            or _int(entry.get("context_window"))
            or _int(entry.get("max_model_len")),
            max_completion_tokens=_int(top.get("max_completion_tokens")),
            catalog_retrieved_at=now,
        )

    # ---------------------------------------------------------------- chat --
    def _payload(
        self, request: GenerationRequest, provider_model_id: str, free_only: bool
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [m.model_dump() for m in request.messages]
        if request.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": messages[-1]["content"]}]
            parts += [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
                for img in request.images
            ]
            messages[-1] = {"role": messages[-1]["role"], "content": parts}
        payload: dict[str, Any] = {
            "model": provider_model_id,
            "messages": messages,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "schema": request.response_schema},
            }
        return payload

    async def chat(
        self,
        request: GenerationRequest,
        provider_model_id: str,
        *,
        timeout_s: float,
        free_only: bool,
    ) -> ProviderReply:
        key = self._key()
        payload = self._payload(request, provider_model_id, free_only)
        try:
            # httpx timeouts are PER PHASE (a read timeout restarts on every
            # byte), and OpenRouter pads slow replies with keep-alive
            # whitespace, so one call ran 344s against a 180s setting. The
            # wall-clock deadline (_within) is the real bound.
            resp = await _within(
                self._client.post(
                    "/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=timeout_s,
                ),
                timeout_s,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise InferenceTimeout(f"{self.name} timed out after {timeout_s:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"{self.name} unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise self._map_http(resp.status_code, resp.text, resp.headers.get("retry-after"), key)
        try:
            body = resp.json()
        except ValueError as exc:
            raise MalformedResponse(f"{self.name} returned non-JSON") from exc
        return self._parse(body, key)

    def _parse(self, body: Any, key: str) -> ProviderReply:
        if not isinstance(body, dict):
            raise MalformedResponse(f"{self.name} returned a non-object body")
        err = body.get("error")
        if err:  # some gateways report failures inside a 200
            code = _dict(err).get("code")
            text = str(_dict(err).get("message", err))
            raise self._map_http(code if isinstance(code, int) else 502, text, None, key)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise MalformedResponse(f"{self.name} returned no choices")
        content = _dict(choices[0].get("message")).get("content")
        if not isinstance(content, str) or not content.strip():
            # finish_reason=length with empty content = the model spent its whole
            # output budget thinking (seen with reasoning models)
            raise MalformedResponse(
                f"{self.name} returned empty content "
                f"(finish_reason={choices[0].get('finish_reason')})"
            )
        usage = _dict(body.get("usage"))
        cost = usage.get("cost")
        served = body.get("model") if isinstance(body.get("model"), str) else None
        upstream = body.get("provider") if isinstance(body.get("provider"), str) else None
        return ProviderReply(
            text=content,
            finish_reason=choices[0].get("finish_reason"),
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            reported_cost_usd=float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None,
            served_model=f"{served} via {upstream}" if served and upstream else served,
        )


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter: OpenAI-compatible plus routing/usage extras. Fallback is
    client-side only — the request never carries OpenRouter's server-side
    ``models`` fallback list, which could route a free-only request to a
    paid model."""

    def _payload(
        self, request: GenerationRequest, provider_model_id: str, free_only: bool
    ) -> dict[str, Any]:
        payload = super()._payload(request, provider_model_id, free_only)
        payload["usage"] = {"include": True}
        prefs: dict[str, Any] = {}
        if request.response_schema is not None:
            prefs["require_parameters"] = True
        if free_only:
            # defence in depth only (not live-verified): admission is the real gate
            prefs["max_price"] = {"prompt": 0, "completion": 0}
        if prefs:
            payload["provider"] = prefs
        return payload


def make_provider(name: str, cfg: CloudProviderConfig) -> OpenAICompatProvider:
    return (OpenRouterProvider if cfg.kind == "openrouter" else OpenAICompatProvider)(name, cfg)
