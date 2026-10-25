"""Security evidence store (Sprint 9): append-oriented, bounded,
scrubbed, provenance-preserving.

- append-only: there is NO update or delete API — historical evidence
  is immutable; when the bounded store is full, appends FAIL CLOSED
  (never silent eviction of audit evidence);
- deterministic ids/sequence: evidence_id is content-derived
  (engagement|target|capability|sequence), the sequence increments
  deterministically;
- secrets are scrubbed BEFORE persistence (Sprint 5 hygiene reuse);
- persistence reuses the same bounded-JSONL envelope pattern as the
  memory store (atomic temp-file replace) — no second persistence
  architecture.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from nomadicos.contracts.security import (
    MAX_EVIDENCE_CONTENT_CHARS,
    EvidenceType,
    SecurityEvidence,
    SecurityRisk,
)
from nomadicos.memory.scrub import scrub

MAX_EVIDENCE_ENTRIES = 512
_STORAGE_TYPE = "security_evidence"


def evidence_id(engagement_id: str, target_id: str, capability: str, sequence: int) -> str:
    """Deterministic evidence identity (replayable)."""
    digest = hashlib.sha256(
        f"{engagement_id}|{target_id}|{capability}|{sequence}".encode()
    ).hexdigest()[:16]
    return f"evt-sec-{digest}"


class EvidenceStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_entries: int = MAX_EVIDENCE_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.path = Path(path) if path is not None else None
        self._max = max_entries
        self._entries: list[SecurityEvidence] = []
        if self.path is not None and self.path.exists():
            self._load()

    # -------------------------------------------------------------- append ---
    def append(
        self,
        *,
        engagement_id: str,
        target_id: str,
        capability: str,
        classification: EvidenceType,
        content: str = "",
        source: str = "",
        confidence: float = 0.5,
        provenance: list[str] | None = None,
        invocation_id: str = "",
        risk: SecurityRisk = SecurityRisk.LOW,
    ) -> SecurityEvidence:
        """Append one scrubbed, bound evidence entry. The store being full
        FAILS CLOSED (audit evidence is never silently evicted)."""
        if len(self._entries) >= self._max:
            raise OverflowError(
                f"evidence store full ({self._max}); refusing to drop audit evidence"
            )
        sequence = len(self._entries)
        # high-risk evidence keeps confidence conservative (never inflated)
        confidence = min(confidence, 0.6) if risk is SecurityRisk.HIGH else confidence
        entry = SecurityEvidence(
            evidence_id=evidence_id(engagement_id, target_id, capability, sequence),
            engagement_id=engagement_id,
            target_id=target_id,
            capability=capability,
            sequence=sequence,
            source=scrub(source)[:128],
            confidence=confidence,
            classification=classification,
            content=scrub(content)[:MAX_EVIDENCE_CONTENT_CHARS],
            provenance=[scrub(ref)[:128] for ref in (provenance or [])[:8]],
            invocation_id=invocation_id,
        )
        self._entries.append(entry)
        self._persist()
        return entry

    # --------------------------------------------------------------- read ---
    def entries(self, engagement_id: str | None = None) -> list[SecurityEvidence]:
        """Deterministic (sequence) order; optional engagement filter."""
        return [
            entry
            for entry in self._entries
            if engagement_id is None or entry.engagement_id == engagement_id
        ]

    def __len__(self) -> int:
        return len(self._entries)

    # -------------------------------------------------------- persistence ---
    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in self._entries:
                    fh.write(
                        json.dumps(
                            {"storage_type": _STORAGE_TYPE, "data": entry.model_dump(mode="json")},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        except OSError:
            return
        loaded: list[SecurityEvidence] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                if raw.get("storage_type") != _STORAGE_TYPE:
                    continue
                loaded.append(SecurityEvidence.model_validate(raw["data"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValidationError, ValueError):
                continue  # corrupt lines are dropped, never crash reads
        self._entries = loaded[: self._max]
