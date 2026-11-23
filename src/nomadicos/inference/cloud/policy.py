"""Cloud admission policy: owner allowlist + ``free_only`` (SPEC §10).

Pure functions over catalog metadata. Price is NEVER discovered by sending a
prompt (a probe can itself be billed and proves nothing about future pricing);
only the refreshed catalog's declared fields count. Anything missing, stale,
negative, unparsable or nonzero excludes the model while ``free_only`` is on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase

from nomadicos.contracts.model import CloudModelMeta, Pricing
from nomadicos.kernel.config import CloudConfig


def qualified_id(provider: str, provider_model_id: str) -> str:
    """Registry/router identity of a cloud model: ``<provider>/<model id>``."""
    return f"{provider}/{provider_model_id}"


def split_qualified(model_id: str) -> tuple[str, str]:
    provider, sep, raw = model_id.partition("/")
    if not sep or not provider or not raw:
        raise ValueError(f"not a provider-qualified cloud model id: {model_id!r}")
    return provider, raw


def matches_any(model_id: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(model_id, p) for p in patterns)


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str = ""


def _zero(raw: str | None) -> bool | None:
    """True = declared zero, False = declared nonzero/negative, None = absent
    or unparsable (ambiguous)."""
    if raw is None:
        return None
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        return None
    if not value.is_finite() or value < 0:  # e.g. OpenRouter dynamic routers use "-1"
        return None
    return value == 0


def free_for_request(pricing: Pricing | None, *, images: bool = False) -> Admission:
    """Both input and output token prices must be declared and zero for the
    requested modality. Per-request / reasoning fees, when declared, must be
    zero too; an undeclared optional fee is not treated as a charge."""
    if pricing is None:
        return Admission(False, "pricing not declared")
    required = {"prompt": pricing.prompt, "completion": pricing.completion}
    if images:
        required["image"] = pricing.image
    for name, raw in required.items():
        state = _zero(raw)
        if state is None:
            return Admission(False, f"{name} price missing or ambiguous")
        if not state:
            return Admission(False, f"{name} price is nonzero")
    for name, raw in (
        ("request", pricing.request),
        ("internal_reasoning", pricing.internal_reasoning),
    ):
        if raw is not None and _zero(raw) is not True:
            return Admission(False, f"{name} price nonzero or ambiguous")
    return Admission(True)


def declared_pricing(meta: CloudModelMeta) -> dict[str, str]:
    if meta.pricing is None:
        return {}
    return {k: v for k, v in meta.pricing.model_dump().items() if v is not None}


def admit(
    meta: CloudModelMeta,
    cfg: CloudConfig,
    *,
    now: datetime,
    structured: bool = False,
    images: bool = False,
    min_context: int = 0,
) -> Admission:
    """Is this model admissible for a request with these needs?

    Used by the router (selection) and again by the engine at call time, so a
    model that went stale or was removed from the allowlist between selection
    and the call is never invoked, and fallback can never reach a model that
    would not be admitted as the primary.
    """
    qid = qualified_id(meta.provider, meta.provider_model_id)
    if meta.provider not in cfg.providers:
        return Admission(False, "provider not configured")
    if not matches_any(qid, cfg.allowed_models):
        return Admission(False, "not in owner allowlist")
    if structured and not meta.structured_output:
        return Admission(False, "structured output not declared")
    if images and "image" not in meta.input_modalities:
        return Admission(False, "image input not declared")
    if meta.context_window is not None:
        if meta.context_window < min_context:
            return Admission(False, f"context {meta.context_window} < required {min_context}")
    elif cfg.free_only:
        return Admission(False, "context window unknown")
    if cfg.free_only:
        age = (now - meta.catalog_retrieved_at).total_seconds()
        if age < 0 or age > cfg.max_catalog_age_s:
            return Admission(False, "catalog metadata stale")
        priced = free_for_request(meta.pricing, images=images)
        if not priced.ok:
            return Admission(False, f"free_only: {priced.reason}")
    return Admission(True)


def estimate_tokens(chars: int, images: int = 0) -> int:
    """Deliberately conservative (3 chars/token): over-estimating only causes
    an early, observable context failure, never a silent truncation."""
    return -(-chars // 3) + 1500 * images
