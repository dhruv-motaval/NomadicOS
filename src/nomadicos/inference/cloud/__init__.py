"""Cloud inference brick (SPEC §9-10): provider adapters, admission policy
(owner allowlist + ``free_only``), bounded retry/fallback, schema-validated
output, and per-request traces. Cloud models are untrusted proposal sources."""

from nomadicos.inference.cloud.engine import CloudEngine
from nomadicos.inference.cloud.policy import Admission, admit, qualified_id, split_qualified
from nomadicos.inference.cloud.providers import (
    OpenAICompatProvider,
    OpenRouterProvider,
    make_provider,
)

__all__ = [
    "Admission",
    "CloudEngine",
    "OpenAICompatProvider",
    "OpenRouterProvider",
    "admit",
    "make_provider",
    "qualified_id",
    "split_qualified",
]
