"""Local HTTP API brick (SPEC §2, §55: the owner layer is UI / CLI / API).

The API is a THIN, OWNER-LOCAL host over the existing bricks. It follows
the same contract as the CLI (`nomadicos.cli.app`):

- it NEVER mints authority: authority artifacts come only from the
  authorization subsystem; owner decisions enter only through the
  documented owner endpoints (grant / revoke / answer / resume);
- it NEVER reports success that verification did not prove: run state is
  the RunSummary produced by NomadicApp (verdicts separate from actions);
- a WAITING_OWNER run is answered ONLY by the owner — the API never
  defaults a conflict (SPEC §5: asked, never silently overridden);
- binding is loopback-only by default (`nomadicos.api.__main__`); this
  surface is for the owner on this machine, not a network service.
"""
