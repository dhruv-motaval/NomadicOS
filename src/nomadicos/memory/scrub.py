"""Deterministic secret exclusion at the memory write boundary (§52G).

Memory is durable context DATA: credentials and runtime secrets must never
be persisted (Sprint 5 provenance rule). One conservative, deterministic
scrubber applied by the store to every written field — content, tags,
properties, and trajectory text. Redacted spans are replaced with a fixed
marker; everything else round-trips verbatim. No network, no model calls,
no authority semantics — pure text hygiene.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

#: keyword[:=]value assignments (password=..., token: ..., api_key=...)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?token"
    r"|refresh[_-]?token|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*\S+"
)

#: PEM private key blocks
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: well-known token shapes (GitHub PAT, AWS access key, Slack bot token)
_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def scrub(text: str) -> str:
    """Redact credential-shaped spans; everything else is unchanged.

    Non-string values pass through untouched (graph properties may be
    numbers/booleans); only credential-shaped strings are redacted."""
    if not isinstance(text, str) or not text:
        return text
    out = _ASSIGNMENT_RE.sub(REDACTED, text)
    out = _PEM_RE.sub(REDACTED, out)
    for pattern in _PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def scrub_all(texts: list[str]) -> list[str]:
    """Scrub a list of strings (tags, evidence refs, summaries)."""
    return [scrub(t) for t in texts]
