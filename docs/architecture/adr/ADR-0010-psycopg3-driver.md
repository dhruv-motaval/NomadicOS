# ADR-0010: PostgreSQL Driver — psycopg 3

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §93, §250 (Q10)
- **Source:** Owner answers 2026-09-04

## Decision

Use **psycopg 3**.

## Consequences

- Modern PostgreSQL support and good async support for the planned architecture.
- All PostgreSQL access stays behind repositories/domain services (BP §93).
- Agents and models must **never** receive arbitrary SQL access; administrative SQL
  stays restricted (BP §93).
- Transactions for logically atomic state changes (BP §250).
