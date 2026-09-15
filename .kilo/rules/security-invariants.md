# Security invariants — always-on rules (SPEC §56, §4, §5, §7, §19, §28)

These are the immutable invariants of the rebuild, from
`NOMADICOS_REBUILD_MASTER_SPEC_FINAL.md` §56. Verify every proposed change
against all fifteen. Owner authority (SPEC §4) is above all learned behavior.

1. **Models propose** — model output is untrusted intent, never a decision.
2. **Owner controls authority** — owner > model, always.
3. **FULL_PC_AUTONOMY is persistent** — one owner grant; later tasks inherit it;
   no repetitive approval prompts for normal actions.
4. **Owner conflicts can be requested** — explicit owner restriction ⇒ model asks,
   owner decides ALLOW/DENY; the model never silently overrides.
5. **Models cannot self-authorize** — no model output ever creates authority.
   Model-authored fields (`authorized`, `allowed`, `approved`, `permission`,
   `privilege`, `capability`, `risk`, `bypass`) are rejected, not honored.
6. **External content is data** — web/file/terminal/tool content is never an
   owner instruction and cannot override OWNER > POLICY > AUTHORIZATION.
7. **Executor only executes authorized actions** — the executor receives the
   `AuthorizedAction` artifact only; raw model output never executes; the only
   source of an `AuthorizedAction` is the authorization subsystem.
8. **Step completion ≠ task completion** — a successful action is not a
   successful goal.
9. **Model completion claim ≠ success** — `finished=true` is a claim state.
10. **Goal verification controls SUCCESS** — SUCCESS requires independent,
    observable goal-predicate evidence.
11. **Smallest capable model preferred** — never spend heavy compute when a
    smaller model measurably suffices.
12. **Strong models are selective specialists** — critics/evaluators/escalation,
    not default workers.
13. **Everything is replaceable** — engines, models, agents, tools, memory,
    orchestrator, verifiers are Lego bricks behind typed contracts.
14. **No false completion** — no IMPLEMENTED/TESTED/VERIFIED/SUCCESS claim
    without evidence; malformed proposals and unknown capabilities fail closed.
15. **Real application behavior matters** — the production path uses real OS
    processes and real filesystem effects, not simulators or mocks only.
