# Project Context

## Engineering Standards

This project follows the engineering standards at:
https://github.com/rmorison/engineering-standards

Key standards applied:

- **[Python Standards](https://github.com/rmorison/engineering-standards/blob/main/code/python-standards.md)** — src-layout, uv, ruff, mypy, pytest, pre-commit
- **[Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md)** — PostgreSQL 18+, per-domain schemas, UUID v7, direct SQL with psycopg 3, golang-migrate, named constraints, database roles
- **[Documentation Standards](https://github.com/rmorison/engineering-standards/blob/main/process/documentation-standards.md)** — Product specs, technical designs, ADRs
- **[Feature Development Workflow](https://github.com/rmorison/engineering-standards/blob/main/process/feature-development-workflow.md)** — Spec-driven: Intent → Spec → Plan → Execute → Validate

## Agent Team

This project runs an agent team (see [Agent Team Workflow](https://github.com/rmorison/engineering-standards/blob/main/process/agent-team-workflow.md)): the operator starts one lead session with that document, and the lead starts each worker with a handoff.

## Project

**Receipt Search Index** — Pre-index receipts from email, extract metadata (vendor, amount, date), generate PDF renditions, provide CLI search for accounting reconciliation.

**Status**: In production. The service is built and runs from the production compose stack in `deploy/` (see Server deployment below).

**Naming**: Only the repository is named `shoebox-filer`. The Python package (`receipt-index` / `receipt_index`), the `receipt-index` CLI, the `RECEIPT_INDEX_*` environment variables and the `receipt_index` database role keep their original names. Don't rename them.

**Tech Stack**: Python 3.11+, PostgreSQL 18+, Anthropic Claude Haiku via Pydantic-AI, Click CLI, Playwright (headless Chromium) for PDF rendering with weasyprint fallback.

**Key Docs**:
- Product spec: `docs/product/features/receipt-search-index-spec.md`
- Technical design: `docs/engineering/designs/receipt-search-index.md`
- ADRs: `docs/engineering/adr/`
- Server deployment (production compose): `docs/user/server-deployment.md` + `deploy/`
- Past problems, root causes and fixes: `docs/solutions/` (check here before debugging a test failure or a dependency issue)

## Local Devtest Setup

### Docker Services

Postgres and GreenMail (mock IMAP for integration tests):
```bash
docker compose up -d
```
Port mappings are configurable via env vars (see `example.env`). The defaults below are Docker Compose's only; the integration tests have none and need all three set:
- `POSTGRES_HOST_PORT` (Compose default 15432)
- `GREENMAIL_SMTP_PORT` (Compose default 3025)
- `GREENMAIL_IMAP_PORT` (Compose default 3143)

### Sharing a Host with Production

On a host that also runs the production stack, the default host ports can collide with production's. Keep the devtest stack apart:

- Set non-default ports and a compose project name in the devtest checkout's `.env`, for example:
  ```bash
  POSTGRES_HOST_PORT=25432
  GREENMAIL_SMTP_PORT=23025
  GREENMAIL_IMAP_PORT=23143
  COMPOSE_PROJECT_NAME=shoebox-dev
  ```
  Docker Compose reads `COMPOSE_PROJECT_NAME` from `.env`, so plain `docker compose` commands and `make docker-up` / `make docker-down` all use the same project, and its containers and volumes stay separate from production's. Passing `docker compose -p shoebox-dev` on every command does the same; the `make docker-*` targets don't pass `-p`. Keep this `.env` to the devtest checkout: an exported `COMPOSE_PROJECT_NAME` overrides the production compose file's own project name.
- Change the port in `DATABASE_URL` and `MIGRATION_DATABASE_URL` in `.env`, and in the migration URL below, from 15432 to your `POSTGRES_HOST_PORT`.
- The integration tests read the three port variables from the environment and have no fallback. If any is unset, empty or not a port number, every integration test skips before a fixture runs or a connection opens. A value that is set is trusted, though: a `.env` copied from `example.env` sets the Compose defaults, so on a shared host change them before running the tests. The Makefile loads `.env`, so `make test-integration` gets them; export them yourself when running `pytest` directly, or if the Makefile can't parse your `.env`. A `.env` alone isn't enough for a direct `pytest` run: the tests decide before anything loads it.

### Database
Migrations (requires [golang-migrate](https://github.com/golang-migrate/migrate)):
```bash
DB="postgresql://main:localpass@localhost:15432/receipt_index_dev?sslmode=disable"  # pragma: allowlist secret
migrate -path db/migrations/public -database "${DB}&x-migrations-table=schema_migrations_public" up
migrate -path db/migrations/receipt -database "${DB}&x-migrations-table=schema_migrations_receipt" up
```
Note: Postgres 18 requires volume mount at `/var/lib/postgresql` (not `/data` subdirectory). Separate migration tables (`schema_migrations_public`, `schema_migrations_receipt`) are needed because both migration dirs start at version 000001.

### Integration Tests

Requires Docker services running, and `POSTGRES_HOST_PORT`, `GREENMAIL_SMTP_PORT` and `GREENMAIL_IMAP_PORT` set to their host ports (from `.env` through the Makefile, or exported). Without them every integration test skips with a message naming the variables:
```bash
docker compose up -d
make test-integration
```

### Running the CLI
See `example.env` for required environment variables. IMAP and Anthropic credentials are stored in 1Password — see project memory for details.
