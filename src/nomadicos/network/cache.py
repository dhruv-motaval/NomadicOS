"""Local web cache with retention (BP §178)."""

import hashlib
import time
from typing import Any

from pydantic import BaseModel, ConfigDict


class CacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    content_hash: str
    retrieved_at_monotonic: float
    body: str
    max_age_seconds: float = 300.0

    def is_fresh(self, now_monotonic: float) -> bool:
        return (now_monotonic - self.retrieved_at_monotonic) < self.max_age_seconds


class WebCache:
    """In-memory cache keyed by URL; hits require a matching fresh entry."""

    def __init__(self, *, default_max_age_seconds: float = 300.0, max_entries: int = 200) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._default_age = default_max_age_seconds
        self._max_entries = max_entries

    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    def get(self, url: str) -> CacheEntry | None:
        entry = self._entries.get(self.url_key(url))
        if entry is None:
            return None
        if not entry.is_fresh(time.monotonic()):
            del self._entries[self.url_key(url)]
            return None
        return entry

    def put(self, url: str, body: str, *, max_age_seconds: float | None = None) -> CacheEntry:
        if len(self._entries) >= self._max_entries:
            # Evict the oldest entry (deterministic, bounded cache).
            oldest_key = min(self._entries, key=lambda k: self._entries[k].retrieved_at_monotonic)
            del self._entries[oldest_key]
        entry = CacheEntry(
            url=url,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
            retrieved_at_monotonic=time.monotonic(),
            body=body,
            max_age_seconds=max_age_seconds or self._default_age,
        )
        self._entries[self.url_key(url)] = entry
        return entry

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict[str, Any]:
        return {"entries": len(self._entries), "max_entries": self._max_entries}


__all__ = ["CacheEntry", "WebCache"]
