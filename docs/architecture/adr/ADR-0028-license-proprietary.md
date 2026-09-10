# ADR-0028: License — Proprietary, Portfolio-Shareable

- **Status:** Accepted
- **Date:** 2026-09-05
- **Resolves:** ADR-0025 open item (license choice)
- **Input:** `docs/architecture/DEPENDENCY_LICENSE_AUDIT.md` + owner decision
  (private project, presented on resume, never public)

## Decision

NomadicOS is **proprietary — all rights reserved**, with an explicit
portfolio-presentation clause (private demonstration and reviewer read-access
at the owner's discretion). No open-source redistribution.

## Consequences

- `LICENSE` file added; `pyproject.toml` declared `license = { text = "Proprietary" }`.
- Resume/portfolio use is explicitly permitted (Section 3) — the project can be
  listed, described, and demoed privately without making the code public.
- ADR-0025 remains valid for everything else (PowerShell bootstrap, audit-first
  posture). The dependency audit showed no license obstruction (psycopg's LGPL
  is satisfied by normal pip use).
- If the owner ever changes their mind, supersede via a new ADR (BP §372.10).

## Future commercialization (owner requirement, 2026-09-05)

The license explicitly reserves the author's exclusive right to **sell or
commercially license** the Software later (LICENSE §5). Requirements verified:

- Code: 100% author-owned (proprietary) — no copyleft contamination (audit §1;
  psycopg LGPL satisfied by unmodified pip use, compatible with commercial sale).
- Model weights (if ever bundled for sale): Gemma and Llama licenses both permit
  commercial use under their terms — but per BP §150-151 the cleaner path is
  keeping weights user-downloaded, which costs nothing and preserves any model
  choice.
- Portfolio clause (§3) is unaffected: the project remains resume-presentable
  today and sellable tomorrow.
