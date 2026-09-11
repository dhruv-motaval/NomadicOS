"""Ollama adapter (ADR-0001: runtime-replaceable LocalModel implementations).

Talks to the local Ollama server (default http://localhost:11434) via its
OpenAI-compatible API. This is STILL local-only inference (I1): the server
runs on the user's machine; no data leaves it. All Ollama specifics stay in
this adapter — never in the Agent Runtime (BP §8.1, §209).
"""

import time

import httpx
from pydantic import ValidationError

from nomadicos.core.errors import (
    FailureClass,
    ModelFailure,
    ModelResourceError,
    ModelUnavailable,
)
from nomadicos.core.logging import get_logger
from nomadicos.models.base import (
    GenerateRequest,
    GenerateResult,
    HealthReport,
    LocalModel,
    ModelCapabilities,
    ModelDescriptor,
    ModelStatus,
    ResourceRequirements,
)

logger = get_logger("models.ollama")

DEFAULT_BASE_URL = "http://localhost:11434"
MAX_BODY_BYTES = 16 * 1024 * 1024
PROBE_MAX_TOKENS = 16

# Known Ollama families → capability map (BP §148 metadata; benchmarked later).
FAMILY_CAPABILITIES: dict[str, dict[str, bool]] = {
    "gemma": {"vision": True, "tool_use": False},  # gemma-3 multimodal (ADR-0004)
    "qwen3-coder": {"vision": False, "tool_use": True},
    "qwen": {"vision": False, "tool_use": True},
    "llama": {"vision": False, "tool_use": False},
    "gpt-oss": {"vision": False, "tool_use": True},
    "nomad-oss": {"vision": False, "tool_use": True},
}


def _capabilities_for(model_id: str) -> ModelCapabilities:
    lowered = model_id.lower()
    for family, caps in FAMILY_CAPABILITIES.items():
        if lowered.startswith(family) or f":{family}" in lowered:
            return ModelCapabilities(
                text_generation=True,
                vision=caps["vision"],
                tool_use=caps["tool_use"],
                max_context_tokens=32768,
            )
    return ModelCapabilities(text_generation=True)


def _classify(exc: httpx.HTTPError) -> FailureClass:
    if isinstance(exc, httpx.TimeoutException):
        return FailureClass.RESOURCE_FAILURE
    return FailureClass.ENVIRONMENT_FAILURE


class OllamaModel(LocalModel):
    """One Ollama-served model as a LocalModel (sequential residency friendly).

    Since Ollama manages loading/unloading internally (ADR-0006), load/unload
    are bookkeeping here; generate() is always available while the server is up.
    """

    def __init__(
        self,
        model_id: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._ollama_name = model_id.split("/", 1)[-1]  # "ollama/gemma-3-4b" → API name
        self._descriptor = ModelDescriptor(
            model_id=model_id,
            display_name=f"Ollama {self._ollama_name}",
            format="ollama",
            status=ModelStatus.DISCOVERED,
            capabilities=_capabilities_for(self._ollama_name),
            resources=ResourceRequirements(estimated_load_seconds=1.0),
            source=f"ollama@{base_url}",
        )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._loaded = False

    @classmethod
    async def discover(
        cls,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list["OllamaModel"]:
        """List models from the local Ollama server (BP §148 discovery).

        Capabilities are verified per model via /api/show (BP §150: metadata is
        untrusted until validated) — a vision claim only sticks if the server
        reports the model actually supports images."""
        models: list[OllamaModel] = []
        async with httpx.AsyncClient(
            base_url=base_url, timeout=30.0, transport=transport
        ) as client:
            response = await client.get("/v1/models")
            response.raise_for_status()
            payload = response.json()
            for entry in payload.get("data", []):
                model = cls(
                    f"ollama/{entry['id']}", base_url=base_url, transport=transport
                )
                await model._verify_capabilities(client)
                await model._verify_digest(client, entry)
                models.append(model)
        logger.info("ollama discovery complete count=%d", len(models))
        return models

    async def _verify_digest(self, client: httpx.AsyncClient, entry: dict) -> None:
        """BP §107/§151: same name + different weights = a different model.

        Ollama exposes the model digest on /api/show; recorded so a re-pulled
        model (same id, new weights) is detected as changed, not silently kept."""
        try:
            response = await client.post("/api/show", json={"model": self._ollama_name})
            response.raise_for_status()
            info = response.json()
            digest = info.get("digest") or info.get("model_info", {}).get("general.digest")
            if digest:
                self._descriptor = self._descriptor.model_copy(
                    update={"checksum_sha256": str(digest)[:64]}
                )
        except Exception as exc:  # noqa: BLE001 — digest unknown ⇒ optional metadata
            logger.warning(
                "digest verification failed model=%s error=%s",
                self._ollama_name,
                type(exc).__name__,
            )

    async def _verify_capabilities(self, client: httpx.AsyncClient) -> None:
        """BP §150/§151: validate capability claims against the server."""
        try:
            response = await client.post(
                "/api/show", json={"model": self._ollama_name}
            )
            response.raise_for_status()
            capabilities = response.json().get("capabilities", [])
            has_vision = "vision" in capabilities
            self._descriptor = self._descriptor.model_copy(
                update={
                    "capabilities": self._descriptor.capabilities.model_copy(
                        update={"vision": has_vision}
                    )
                }
            )
        except Exception as exc:  # noqa: BLE001 — capability unknown ⇒ no vision claim
            logger.warning(
                "capability verification failed model=%s error=%s",
                self._ollama_name,
                type(exc).__name__,
            )
            self._descriptor = self._descriptor.model_copy(
                update={
                    "capabilities": self._descriptor.capabilities.model_copy(
                        update={"vision": False}
                    )
                }
            )

    # ---------------------------------------------------------------- LocalModel

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True  # Ollama handles residency (ADR-0006)

    async def unload(self) -> None:
        self._loaded = False

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
            )
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        started = time.monotonic()
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                json={
                    "model": self._ollama_name,
                    "messages": messages,
                    "max_tokens": request.max_output_tokens,
                    "temperature": request.temperature,
                    **({"stop": request.stop} if request.stop else {}),
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ModelUnavailable(
                    f"model not served: {self._ollama_name}",
                    context={"base_url": self._base_url},
                ) from exc
            raise ModelFailure(
                f"ollama HTTP {exc.response.status_code}",
                context={"model": self._ollama_name},
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelFailure(
                f"ollama transport failure: {_classify(exc).value}",
                context={"model": self._ollama_name},
            ) from exc
        if len(response.content) > MAX_BODY_BYTES:
            raise ModelFailure(
                "ollama response too large",
                context={"model": self._ollama_name},
            )
        try:
            payload = response.json()
        except ValidationError as exc:
            raise ModelFailure(
                "ollama invalid JSON", context={"model": self._ollama_name}
            ) from exc
        latency_ms = (time.monotonic() - started) * 1000
        choice = (payload.get("choices") or [{}])[0]
        usage = payload.get("usage") or {}
        return GenerateResult(
            text=(choice.get("message") or {}).get("content", ""),
            finish_reason=choice.get("finish_reason"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
        )

    async def health(self) -> HealthReport:
        try:
            result = await self.generate(
                GenerateRequest(prompt="ping", max_output_tokens=PROBE_MAX_TOKENS)
            )
        except (ModelFailure, ModelUnavailable, ModelResourceError) as exc:
            return HealthReport(healthy=False, detail=str(exc))
        return HealthReport(healthy=True, latency_ms=result.latency_ms)

    # -------------------------------------------------- VisionModel capability
    # ADR-0004: a multimodal LocalModel satisfies VisionModel. gemma-3 family is
    # multimodal; image bytes go to the LOCAL server as base64 — never off-machine.

    async def describe(
        self,
        image_bytes: bytes,
        question: str | None = None,
        *,
        max_tokens: int = 512,
    ) -> str:
        """Describe a screenshot locally via Ollama /api/chat with images (BP §51)."""
        if not self._descriptor.capabilities.vision:
            raise ModelResourceError(
                f"model {self._ollama_name} has no vision capability",
                context={"model": self._ollama_name},
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                # vision requests can include cold model load time (BP §153)
                timeout=httpx.Timeout(max(self._timeout, 300.0)),
                transport=self._transport,
            )
        import base64

        started = time.monotonic()
        try:
            response = await self._client.post(
                "/api/chat",
                json={
                    "model": self._ollama_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": question
                            or "Describe this screen: visible windows, buttons, "
                            "text fields, and their approximate positions.",
                            "images": [base64.b64encode(image_bytes).decode("ascii")],
                        }
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelFailure(
                f"ollama vision HTTP {exc.response.status_code}",
                context={"model": self._ollama_name},
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelFailure(
                "ollama vision transport failure", context={"model": self._ollama_name}
            ) from exc
        try:
            payload = response.json()
        except ValidationError as exc:
            raise ModelFailure(
                "ollama vision invalid JSON", context={"model": self._ollama_name}
            ) from exc
        content = (payload.get("message") or {}).get("content", "")
        logger.info(
            "vision described model=%s chars=%d latency_ms=%.1f",
            self._ollama_name,
            len(content),
            (time.monotonic() - started) * 1000,
        )
        return content

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["DEFAULT_BASE_URL", "OllamaModel"]
