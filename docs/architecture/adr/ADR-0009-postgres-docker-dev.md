# ADR-0009: PostgreSQL Deployment — Docker Compose for Development

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §76, §130, §173 (Q9)
- **Source:** Owner answers 2026-09-04

## Decision

Use **Docker Compose for development** (`docker-compose.dev.yml`). Production/local-user
installation should eventually support a setup abstraction rather than hard-coding
Docker permanently.

## Consequences

- PostgreSQL data never goes inside the Git repository.
- Database files are never kept under OneDrive/sync-heavy directories (see
  environment configuration; repo relocation recommended).
- Migration tooling lands in Phase 1 alongside the first schema (BP §104).
