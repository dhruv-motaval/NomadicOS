# Security invariants — always-on rules (BP §4.2, §190, ADR-0013)

These are the immutable security invariants. They are enforced in code
(`constitution/invariants.py`) and by the invariant test suite (BP §191). No policy,
model, agent, or owner workflow can override them. Verify every proposed change against
all fifteen.

## I1. Local-only inference
No external LLM/API inference calls may exist in v0.1 (BP §1.3, §200, §286).

## I2. No OmniRouter / Hermes / Skill System
Never reintroduce components removed by BP §1.1 or add a separate Skill System (BP §82, §370).

## I3. No privilege escalation
The model can never grant itself permissions, roles, or capabilities (BP §42).

## I4. No policy modification
The model can never alter immutable policies; owner policy changes require explicit
human action through the Policy Engine (BP §42, §260).

## I5. No gate bypass
All effectful actions pass through the Tool Gateway and Security Gate (BP §11-13, §73).
No side doors: no direct DB SQL from agents (BP §93), no raw network from tools.

## I6. Fail closed
Unknown permission, unknown tool, invalid policy, uncertain classification ⇒ BLOCK (BP §85).
Invalid policy configuration fails startup (ADR-0013).

## I7. No audit tampering
Audit is append-only and never model-controlled (BP §41-42, ADR-0020). The model can
never delete or alter audit history (BP §82).

## I8. No evidence fabrication
Never report unverified success; hide failures; fabricate tool output (BP §69, §366).

## I9. No unauthorized persistence
No hidden processes, no self-installed services, no replication (BP §68).

## I10. Bounded execution
All loops, retries, durations, and resources are budgeted; budgets are enforced
outside the model (BP §52, §70, §72).

## I11. Data locality
Private data (files, screenshots, memory, credentials, datasets, task history) never
leaves the machine — public-web GETs are the only permitted external traffic in v0.1
(BP §199-200, answers Section J).

## I12. Secrets containment
No plaintext secrets in logs, audit, prompts, memory, experience records, or benchmark
traces (BP §256, ADR-0021).

## I13. Supply-chain validation
Models and extensions are untrusted artifacts until verified (checksum, manifest,
permissions, scan) (BP §150-152).

## I14. Learnable ≠ authoritative
Learned policy never overrides immutable policy (BP §190); cross-session experience is
evidence to revalidate, not truth (BP §385, §416).

## I15. User authority above all learned behavior
User control outranks model preference and learned policy, while these invariants remain
enforced (BP §367; precedence per §262).
