# ADR-0031: PostgreSQL Deployment — Native Install with pgAdmin (Owner Preference)

- **Status:** Accepted (supersedes the Docker-first part of ADR-0009; Docker remains optional)
- **Date:** 2026-09-07
- **Owner decision:** native Windows install with GUI management — Docker is too complex for the owner's workflow.

## Decision

- **PostgreSQL 17**, native Windows install via the official EDB installer
  (includes **pgAdmin 4** — a full web-based GUI for databases, queries, and backups).
- NomadicOS connects to `localhost:5432` exactly as before — **no code changes**:
  same `CoreConfig`, same repositories, same migrations (001–003 auto-apply on startup).
- `docker-compose.dev.yml` is kept as an **optional alternative** for reproducible CI
  environments, but is no longer the owner's primary path (ADR-0009 superseded in part).

## Consequences

- Owner installs once via GUI; no Docker Desktop dependency (also removes the
  Docker-must-run precondition for integration tests — they hit localhost instead).
- Data directory lives outside OneDrive (BP §103, ADR-0009): EDB default
  `C:\Program Files\PostgreSQL\17\data` qualifies.
- Product packaging (ADR-0029 roadmap) follows the same path for buyers:
  guided native install instead of Docker.
