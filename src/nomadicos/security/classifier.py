"""Deterministic data classifier (BP §264-266, ADR-0019).

Path heuristics, regex secret patterns, extensions, entropy; classification is
conservative — uncertain ⇒ stricter.
"""

import math
import re
from pathlib import PurePosixPath
from typing import Literal

Classification = Literal["public", "internal", "sensitive"]

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"(?i)password\s*[=:]\s*\S+"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
)

SENSITIVE_NAME_MARKERS = (
    "credential",
    "secret",
    "password",
    "wallet",
    "private",
    "apikey",
    "api_key",
    "token",
    ".ssh",
    "id_rsa",
    ".kube",
)
SENSITIVE_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".env"}
PUBLIC_EXTENSIONS = {".md", ".txt", ".rst"}


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in freq.values())


def classify_content(content: str) -> Classification:
    """Classify file/text content. Conservative: uncertain ⇒ sensitive (BP §266)."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            return "sensitive"
    if _entropy(content[:4096]) > 5.0 and len(content) > 64:
        return "sensitive"  # high-entropy blob: assume secret material
    return "internal"


def classify_path(path: str) -> Classification:
    """Classify by path heuristics (BP §265)."""
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name.lower()
    suffix = posix.suffix.lower()
    parts = [p.lower() for p in posix.parts]

    if suffix == ".env" or name.startswith(".env"):
        return "sensitive"
    if suffix in SENSITIVE_EXTENSIONS:
        return "sensitive"
    for marker in SENSITIVE_NAME_MARKERS:
        if marker in name:
            return "sensitive"
    if any(part in (".ssh", ".kube", ".aws") for part in parts):
        return "sensitive"
    if any(part in ("windows", "system32") for part in parts):
        return "sensitive"
    if suffix in PUBLIC_EXTENSIONS:
        return "public"
    return "internal"


def classify(path: str, content: str | None = None) -> Classification:
    """Combined classification; strictest of both signals wins (BP §266)."""
    path_class = classify_path(path)
    if content is None:
        return path_class
    content_class = classify_content(content)
    order: dict[str, int] = {"public": 0, "internal": 1, "sensitive": 2}
    return path_class if order[path_class] >= order[content_class] else content_class


__all__ = [
    "classify",
    "classify_content",
    "classify_path",
]
