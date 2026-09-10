# ADR-0029: NomadicOS Will Be Sold as a Product

- **Status:** Accepted
- **Date:** 2026-09-05
- **Builds on:** ADR-0028 (proprietary license, commercial rights reserved)
- **Owner decision:** distribution intent = commercial product

## Decision

NomadicOS will be sold as a commercial product. This changes engineering
priorities (packaging, docs, support surface) but **not the architecture** —
which is already product-shaped:

| Product advantage | Blueprint origin |
|---|---|
| **Privacy as a feature** — zero data leaves the machine | BP §1.3, §199-200, I11 |
| **Zero per-token cost** — user's own local models | BP §1.3, ADR-0001/0006 |
| **No vendor lock-in** — models, tools, vector engine replaceable | BP §371, ADR-0001/0007 |
| **Auditable, reversible actions** — trust story for buyers | BP §41-42, §45, §289 |
| **Cross-session memory** — actual assistant value | BP §376-420 |

## Immediate consequences (no-regret engineering)

1. `README.md` must exist and be product-quality (resume/portfolio doubles as
   the marketing artifact). pyproject already references it.
2. Distribution plan tracked in `docs/architecture/PRODUCT_ROADMAP.md`:
   installer/setup wizard (BP §130, ADR-0025), EULA (replaces the internal
   LICENSE for buyers), optional license-key activation, support boundary.
3. PostgreSQL inside the product needs the setup abstraction (ADR-0009
   anticipated this): embedded/portable PG or guided install — Docker stays
   developer-only.
4. Zero telemetry by default — it is the product's core promise (I11).

## Not decided by this ADR

Pricing, payment/distribution channel, product name/trademark, EULA text
(legal review), target customer. These are business decisions recorded in the
product roadmap when made.
