# ADR-0001: PostgreSQL for Search Index Storage

## Status

Accepted

## Context

The receipt search index needs a database to store extracted metadata (vendor, amount, date) and support search queries with filtering by vendor substring, amount range, and date range. The system also needs to track processed message IDs for idempotent ingestion.

Key factors:

- **Query requirements**: Substring search on vendor names, range queries on amounts and dates, combined filters
- **Volume**: ~200 existing receipts, growing at ~10-20 per week — modest scale
- **Deployment**: Runs locally, single-user CLI tool
- **Future considerations**: Possible web UI, multi-user access, full-text search across receipt content
- **Engineering standards**: [Database standards](../../../engineering-standards/code/database-standards.md) designate PostgreSQL as the default engine with direct SQL via psycopg 3

## Decision

Use **PostgreSQL 18+** as the metadata store and search index, following the [database standards](../../../engineering-standards/code/database-standards.md) for schema design, naming conventions, migrations (golang-migrate), roles, and data access (direct SQL with psycopg 3).

## Consequences

### Positive

- **Rich query capabilities**: Native support for `ILIKE` substring matching, range queries, and combined filters without additional tooling
- **Full-text search**: `tsvector`/`tsquery` available if we later want to search receipt content, not just structured fields
- **`pg_trgm` extension**: Trigram indexes for fast vendor substring search
- **UUID v7**: Native `uuidv7()` in PostgreSQL 18+ — time-ordered, globally unique primary keys with no extension required
- **Concurrency**: Handles concurrent reads/writes cleanly if a web UI or background ingestion is added later
- **Ecosystem**: Mature tooling — psycopg 3 for direct SQL, golang-migrate for schema evolution, Pydantic for data validation
- **Standards alignment**: Consistent with database standards (schema organization, named constraints, role-based access, column-level privileges)
- **Docker Compose**: Easy to run alongside the application with a single `docker compose up`

### Negative

- **Operational overhead vs. SQLite**: Requires a running PostgreSQL instance (mitigated by Docker Compose)
- **Heavier for single-user CLI**: SQLite would be zero-config for a purely local tool
- **Setup friction**: New developers need Docker or a local PostgreSQL install
- **golang-migrate binary**: External tool dependency outside the Python ecosystem (mitigated by simple install via `go install` or pre-built binaries)

### Alternatives Considered

- **SQLite**: Zero-config, embedded, sufficient for current volume. Not chosen because substring search and full-text search are weaker, and migration to PostgreSQL later would be more disruptive than starting with it.
- **SQLite with FTS5**: Better full-text search than vanilla SQLite, but still lacks PostgreSQL's query flexibility, schema organization, and concurrency model.
- **SQLAlchemy/Alembic**: ORM-based approach with Python-native migrations. Not chosen — database standards mandate direct SQL with psycopg 3 and golang-migrate for language-agnostic, ORM-decoupled migrations.

## Follow-ups

- Define database schema in technical design (domain schema `receipt`, named constraints, roles)
- Set up golang-migrate with per-schema migration directories
- Provide Docker Compose configuration for PostgreSQL 18
