# Product Roadmap — NomadicOS Commercial Distribution

> Engineering tracking for ADR-0029. Business decisions (pricing, channel,
> trademark) are recorded here when made; legal items need review.

## Current state (v0.1-internal)

Architecture complete; runs on the owner's machine against a local Ollama
fleet; 243 tests; proprietary license with portfolio clause.

## Gap analysis: internal project → sellable product

### 1. Packaging & installation (BP §130, ADR-0009 setup abstraction)

- [ ] Single-installer story: PowerShell bootstrap → embedded Python (no
      user-side Python knowledge required) → dependency install
- [ ] PostgreSQL decision: embedded portable PG **or** guided Docker install
      **or** SQLite fallback for the non-technical tier (schema is simple;
      ADR required if SQLite)
- [ ] Ollama detection/install handoff (already installed on target users? or
      bundled offline?)
- [ ] First-run wizard: model pull (`gemma3:4b` minimum), workspace dir,
      policy selection (conservative default), owner account
- [ ] Health check page/CLI (BP §130 final item) — one command proving the
      whole stack works

### 2. Legal & commercial (ADR-0025/0028)

- [ ] EULA for buyers (distinct from the internal LICENSE; legal review)
- [ ] License-key or account activation mechanism (piracy posture: local-first
      products are cracked easily — decide effort level; offline-friendly
      signed license files are the natural fit)
- [ ] Trademark check for the product name "NomadicOS"
- [ ] Dependency license compliance shipped as `THIRD_PARTY_NOTICES` (audit is
      done: `docs/architecture/DEPENDENCY_LICENSE_AUDIT.md`)

### 3. Product surface (ADR-0014 UI phase — owner deferred, now unlocked)

- [ ] Local web dashboard: task dashboard, ASK-approval queue, memory manager,
      model health, audit view (BP §54-55, §279-281)
- [ ] Onboarding flow in the dashboard
- [ ] Update mechanism (signed bundles; BP §152 supply-chain rules apply to
      NomadicOS's own updates)

### 4. Robustness for strangers' machines

- [ ] HardwareProfile-aware UX: refuse-and-explain when a model cannot run
      (BP §170/§53) instead of internal errors
- [ ] Crash-safe telemetry-free error reporting: exportable bug bundle
      (redacted logs, BP §256) the user chooses to send
- [ ] Test matrix: clean Windows 10/11 VMs, no-Docker machines, CPU-only
- [ ] Security review pass by a second pair of eyes (BP §299 was self-applied)

### 5. Documentation for buyers

- [ ] User guide (non-developer voice) — current docs are engineering-grade
- [ ] Policy recipes: "personal assistant", "developer agent", "restricted"
- [ ] Video: first-run to first completed task

## Sequencing recommendation

```text
v0.1  (now)      internal, architecture complete
v0.2             packaging + installer + first-run wizard        ← start here
v0.3             dashboard (task/approvals/memory) + EULA
v0.4             hardening for strangers' machines + user guide
v1.0             sale-ready: activation, notices, support boundary
```

## Non-negotiables that survive commercialization

Local-only inference, fail-closed security gate, append-only audit,
evidence-based verification, model/tool replaceability, rollback (BP §368-371).
These are the product's differentiators — do not trade them for speed.
