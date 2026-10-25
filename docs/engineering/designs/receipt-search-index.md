# Technical Design: Receipt Search Index

**Version:** 0.2 — Draft
**Date:** 2026-02-16
**Product Spec:** [receipt-search-index-spec.md](../../product/features/receipt-search-index-spec.md)

---

## Context

The product spec defines a tool that pre-indexes receipts from email, extracts structured metadata (vendor, amount, date), generates PDF renditions, and provides fast CLI-based search. The reconciler's workflow changes from "hunt for the receipt" to "confirm the match."

This design covers **Phase 1** — email receipt ingestion from an IMAP folder. It defines the system components, data models, interfaces, and implementation sequence needed to deliver a working CLI tool.

### Key Technical Challenges

1. **Email format diversity** — Receipts arrive as forwarded HTML, attached PDFs, inline images, plain-text confirmations. The ingestion pipeline must normalize all of these.
2. **Metadata extraction accuracy** — Vendor name, amount, and date must be reliably extracted from unstructured email content.
3. **PDF rendition quality** — Generated PDFs must be clean enough for a bookkeeper to attach to accounting transactions without rework.
4. **Idempotent processing** — Re-running ingestion must skip already-processed messages without duplicating data.

### Architectural Decisions

- [ADR-0001: PostgreSQL for Search Index](../adr/0001-postgresql-for-search-index.md)
- [ADR-0002: LLM Extraction via Pydantic-AI](../adr/0002-llm-extraction-via-pydantic-ai.md)
- [ADR-0003: Local File Store with Abstraction](../adr/0003-local-file-store-with-abstraction.md)

### Standards

- [Database Standards](../../../engineering-standards/code/database-standards.md)
- [Python Standards](../../../engineering-standards/code/python-standards.md)

---

## Approach

### Solution Overview

A Python CLI application with three main operations:

1. **`ingest`** — Connect to IMAP, fetch unprocessed messages, extract metadata via LLM, render PDF, store both metadata and PDF
2. **`search`** — Query the PostgreSQL index by vendor, amount, and/or date range; return results with PDF paths
3. **`show`** — Display full metadata for a specific receipt

The system is designed as a pipeline with clearly separated concerns: source adapters, metadata extraction, PDF rendering, storage, and search.

### Alternatives Considered

- **Web application with background workers**: Adds deployment complexity (web server, task queue, worker processes) without clear benefit for a single-user CLI tool. The product spec explicitly calls for one-shot CLI execution with future workflow system integration (e.g., Dagster).
- **Monolithic script**: Simpler but harder to extend for Phase 2 (Google Drive) and harder to test. The adapter pattern adds minimal complexity while keeping the pipeline source-agnostic.

---

## Design

### Project Structure

Per [Python standards](../../../engineering-standards/code/python-standards.md) (src-layout) and [database standards](../../../engineering-standards/code/database-standards.md) (per-schema migration directories):

```
receipt-index/
├── src/
│   └── receipt_index/
│       ├── __init__.py
│       ├── py.typed
│       ├── cli.py                  # CLI entry point (Click)
│       ├── config.py               # Environment variable loading
│       ├── models.py               # Pydantic models (domain + extraction)
│       ├── db.py                   # Database connection and queries (direct SQL, psycopg 3)
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py             # Source adapter protocol
│       │   └── imap.py             # IMAP source adapter
│       ├── extraction.py           # LLM-based metadata extraction
│       ├── renderer.py             # Email-to-PDF rendition
│       └── store.py                # File store abstraction + local impl
├── db/
│   └── migrations/
│       ├── public/                 # Shared utilities (set_updated_at trigger)
│       │   ├── 000001_create_set_updated_at_function.up.sql
│       │   └── 000001_create_set_updated_at_function.down.sql
│       └── receipt/                # Receipt domain schema
│           ├── 000001_create_receipts_table.up.sql
│           ├── 000001_create_receipts_table.down.sql
│           ├── 000002_grant_receipts_access.up.sql
│           └── 000002_grant_receipts_access.down.sql
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
├── docker-compose.yml
├── pyproject.toml
├── Makefile
└── ...
```

### Components

#### 1. CLI (`cli.py`)

Entry point for all operations. Uses Click for command parsing.

```
receipt-index ingest [--dry-run] [--limit N]
receipt-index search [--vendor TEXT] [--amount DECIMAL] [--amount-min DECIMAL] [--amount-max DECIMAL] [--date-from DATE] [--date-to DATE] [--output json|text]
receipt-index show <receipt-id> [--output json|text]
```

- `--output json` emits structured JSON (Pydantic model serialization) for agent consumption (NFR-08)
- `--output text` (default) emits human-friendly tabular output
- `--dry-run` on ingest shows what would be processed without writing to DB or file store

#### 2. Source Adapter (`adapters/`)

Protocol-based interface for receipt sources:

```python
@runtime_checkable
class SourceAdapter(Protocol):
    def fetch_unprocessed(self, processed_ids: set[str]) -> Iterator[RawReceipt]: ...
```

```python
@dataclass
class RawReceipt:
    source_id: str          # Unique ID (IMAP message ID)
    subject: str            # Email subject
    sender: str             # From address
    date: datetime          # Email date
    html_body: str | None   # HTML content
    text_body: str | None   # Plain text content
    attachments: list[Attachment]  # Attached files

@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes
```

**IMAP adapter** (`adapters/imap.py`):
- Connects via `imaplib` (stdlib) with SSL
- Fetches all messages from the configured folder
- Filters out already-processed IDs (passed in from DB)
- Parses each message into `RawReceipt` using `email` stdlib
- Handles MIME multipart messages, inline images, and attachments

#### 3. Metadata Extraction (`extraction.py`)

Uses Pydantic-AI with Anthropic Haiku to extract structured metadata:

```python
class ReceiptMetadata(BaseModel):
    vendor: str              # Vendor/merchant name
    amount: Decimal          # Transaction amount
    currency: str = "USD"    # Currency code
    date: date               # Transaction date
    description: str | None  # Brief description of purchase
    confidence: float        # Extraction confidence (0-1)
```

**Extraction strategy**:
1. Concatenate email subject, sender, text body (or stripped HTML) into a single prompt context
2. System prompt instructs the model to extract vendor, amount, date from receipt content
3. Pydantic-AI validates output against `ReceiptMetadata` schema
4. If HTML body is present, strip tags for text extraction (keep HTML for PDF rendering)
5. For emails with PDF attachments, extract text from the PDF for LLM input

**Prompt design considerations**:
- Include sender address as a hint (e.g., `noreply@amazon.com` → vendor is Amazon)
- Handle forwarded emails where the original sender info is in the body
- Handle multi-item receipts by extracting the total amount
- Return confidence score so low-confidence extractions can be flagged

#### 4. PDF Renderer (`renderer.py`)

Generates a single human-readable PDF per receipt:

- **HTML emails**: Render HTML body to PDF using `weasyprint` (or `pdfkit`/`wkhtmltopdf`)
- **Emails with PDF attachments**: Use the attached PDF directly (copy to file store)
- **Plain text emails**: Wrap in minimal HTML template, render to PDF
- **Inline images**: Embed as data URIs in HTML before rendering

The renderer produces the final PDF bytes; the file store handles naming and placement.

**Library choice**: `weasyprint` — pure Python, no external binary dependencies (unlike `wkhtmltopdf`), good CSS support for email HTML rendering.

#### 5. File Store (`store.py`)

Per [ADR-0003](../adr/0003-local-file-store-with-abstraction.md):

```python
class FileStore(Protocol):
    def save(self, receipt_date: date, vendor: str, amount: Decimal, pdf_data: bytes) -> str: ...
    def get_path(self, relative_path: str) -> Path: ...
    def exists(self, relative_path: str) -> bool: ...

class LocalFileStore:
    def __init__(self, root: Path) -> None: ...

    def save(self, receipt_date: date, vendor: str, amount: Decimal, pdf_data: bytes) -> str:
        """Save PDF and return relative path from store root."""
        slug = self._slugify_vendor(vendor)
        dir_path = self.root / str(receipt_date.year) / f"{receipt_date.month:02d}"
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"{receipt_date.isoformat()}__{slug}__{amount}.pdf"
        file_path = dir_path / filename
        # Handle duplicates by appending numeric suffix
        file_path.write_bytes(pdf_data)
        return str(file_path.relative_to(self.root))
```

Directory layout: `{root}/{YYYY}/{MM}/{YYYY-MM-DD}__{vendor}__{amount}.pdf`

#### 6. Database (`db.py`)

Direct SQL with psycopg 3 per [database standards](../../../engineering-standards/code/database-standards.md). No ORM — Pydantic models handle validation and serialization, psycopg handles query execution.

```python
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row


class Receipt(BaseModel):
    id: UUID
    source_id: str
    source_type: str
    vendor: str
    amount: Decimal
    currency: str
    receipt_date: date
    description: str | None
    confidence: float
    pdf_path: str
    email_subject: str | None
    email_sender: str | None
    email_date: datetime | None
    created_at: datetime
    updated_at: datetime


def insert_receipt(conn: psycopg.Connection, metadata: ReceiptMetadata, ...) -> Receipt:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO receipt.receipts
                (source_id, source_type, vendor, amount, currency,
                 receipt_date, description, confidence, pdf_path,
                 email_subject, email_sender, email_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (source_id, source_type, vendor, amount, ...),
        )
        row = cur.fetchone()
        return Receipt.model_validate(row)


def search_receipts(
    conn: psycopg.Connection,
    vendor: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Receipt]:
    """Build and execute search query with parameterized filters."""
    ...
```

### Data Model

#### Schema: `receipt`

Per database standards, tables are organized into a per-domain schema. This project has a single domain.

```
public (shared utilities)
  └── receipt (depends on public)
```

#### PostgreSQL Extensions

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- Trigram matching for vendor substring search
```

UUID v7 is provided natively by PostgreSQL 18+ via `uuidv7()` — no extension required.

#### Shared Utilities (`public` schema)

```sql
-- Trigger function for auto-updating updated_at
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

#### `receipt.receipts` Table

```sql
CREATE SCHEMA IF NOT EXISTS receipt;

CREATE TABLE IF NOT EXISTS receipt.receipts (
    id              UUID NOT NULL DEFAULT uuidv7(),
    source_id       TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'imap',
    vendor          TEXT NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    receipt_date    DATE NOT NULL,
    description     TEXT,
    confidence      REAL NOT NULL,
    pdf_path        TEXT NOT NULL,
    email_subject   TEXT,
    email_sender    TEXT,
    email_date      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_receipts PRIMARY KEY (id),
    CONSTRAINT uq_receipts_source_id UNIQUE (source_id),
    CONSTRAINT ck_receipts_amount_positive CHECK (amount > 0),
    CONSTRAINT ck_receipts_confidence_range CHECK (confidence >= 0 AND confidence <= 1)
);

-- Search indexes
CREATE INDEX IF NOT EXISTS idx_receipts_vendor
    ON receipt.receipts USING gin (vendor gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_receipts_amount
    ON receipt.receipts (amount);
CREATE INDEX IF NOT EXISTS idx_receipts_receipt_date
    ON receipt.receipts (receipt_date);

-- Auto-update updated_at on row modification
CREATE TRIGGER trg_receipts_set_updated_at
    BEFORE UPDATE ON receipt.receipts
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();
```

**Notes**:
- `gin_trgm_ops` index enables fast `ILIKE '%substring%'` vendor search (requires `pg_trgm` extension)
- `uq_receipts_source_id` constraint enforces idempotency (FR-08)
- `pdf_path` stores the relative path from the file store root, not an absolute path — portable across environments
- All constraints are explicitly named per database standards
- `id`, `created_at`, `updated_at` are database-managed — application code omits them on insert

#### Database Roles

Per database standards, three roles with the naming convention `{app}_{env}_{level}`:

| Role | Purpose |
|------|---------|
| `receipt_index_dev_all` | Runs migrations, manages schema objects (DDL) |
| `receipt_index_dev_write` | Application connection — CRUD on business columns only |
| `receipt_index_dev_read` | Debugging, ad-hoc queries — SELECT only |

Column-level privileges on `receipt.receipts` ensure the `_write` role cannot supply `id`, `created_at`, or `updated_at`:

```sql
-- _write: CRUD on business columns only
GRANT USAGE ON SCHEMA receipt TO receipt_index_dev_write;
GRANT SELECT ON receipt.receipts TO receipt_index_dev_write;
GRANT INSERT (source_id, source_type, vendor, amount, currency,
              receipt_date, description, confidence, pdf_path,
              email_subject, email_sender, email_date)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT UPDATE (vendor, amount, currency, receipt_date, description,
              confidence, pdf_path)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT DELETE ON receipt.receipts TO receipt_index_dev_write;

-- _read: SELECT only
GRANT USAGE ON SCHEMA receipt TO receipt_index_dev_read;
GRANT SELECT ON receipt.receipts TO receipt_index_dev_read;
```

#### Migrations

Managed by [golang-migrate](https://github.com/golang-migrate/migrate) with per-schema directories under `db/migrations/`. Migrations run in schema dependency order via Makefile:

```makefile
.PHONY: migrate-up
migrate-up:  ## Run all migrations in schema dependency order
	migrate -path db/migrations/public -database "$(DATABASE_URL)" up
	migrate -path db/migrations/receipt -database "$(DATABASE_URL)" up

.PHONY: migrate-down
migrate-down:  ## Roll back the last migration for each schema (reverse order)
	migrate -path db/migrations/receipt -database "$(DATABASE_URL)" down 1
	migrate -path db/migrations/public -database "$(DATABASE_URL)" down 1
```

### Data Flow

#### Ingestion Pipeline

```
1. CLI invokes `ingest` command
2. Load processed source_ids from DB
3. IMAP adapter fetches unprocessed messages
4. For each RawReceipt:
   a. Extract metadata via LLM → ReceiptMetadata
   b. Render email to PDF → bytes
   c. Save PDF to file store → relative_path
   d. Insert metadata + pdf_path into receipt.receipts
   e. Log result (success/failure + confidence)
5. Report summary: processed, skipped, failed
```

```
┌──────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌──────────┐
│ IMAP │───→│  Adapter  │───→│ Extractor │───→│ Renderer │───→│  Store   │
│Server│    │(fetch msg)│    │  (LLM)    │    │(to PDF)  │    │(save PDF)│
└──────┘    └──────────┘    └───────────┘    └──────────┘    └────┬─────┘
                                                                  │
                                                                  ▼
                                                             ┌──────────┐
                                                             │ Postgres │
                                                             │(metadata)│
                                                             └──────────┘
```

#### Search Flow

```
1. CLI parses search filters (vendor, amount, date range)
2. Build parameterized SQL query with applicable WHERE clauses
3. Execute query against receipt.receipts
4. Format results as text table or JSON
5. Return to stdout
```

### Configuration

Per Python standards, environment variables loaded via `python-dotenv`:

```bash
# IMAP
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=receipts@example.com
IMAP_PASSWORD=app-specific-password
IMAP_FOLDER=INBOX

# Database
DATABASE_URL=postgresql://receipt_index_dev_write:localpass@localhost:5432/receipt_index_dev

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# File Store
RECEIPT_STORE_PATH=./data/receipts

# Optional
LOG_LEVEL=INFO
LLM_MODEL=claude-haiku-4-5-20251001
```

### Dependencies

**Runtime**:

| Package | Purpose |
|---------|---------|
| `click` | CLI framework |
| `pydantic` | Data models and validation |
| `pydantic-ai` | LLM orchestration with structured output |
| `anthropic` | Anthropic API client (Pydantic-AI dependency) |
| `psycopg[binary]` | PostgreSQL driver (psycopg 3, direct SQL) |
| `python-dotenv` | Environment variable loading |
| `weasyprint` | HTML-to-PDF rendering |
| `python-slugify` | Vendor name slugification |

**Dev** (per Python standards):

| Package | Purpose |
|---------|---------|
| `pytest`, `pytest-cov` | Testing and coverage |
| `ruff` | Linting and formatting |
| `mypy` | Type checking |
| `pre-commit` | Git hooks |
| `detect-secrets` | Secret scanning |
| `pip-audit` | Vulnerability scanning |

**External tooling** (not Python packages):

| Tool | Purpose |
|------|---------|
| `golang-migrate` | Database migrations (per database standards) |
| `docker compose` | Local PostgreSQL 18 |

---

## Implementation Plan

### Phase 1a: Foundation (8 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| Project scaffolding (pyproject.toml, Makefile, CI, Docker Compose) | 2 | None |
| Data models (Pydantic models for domain + extraction) | 2 | None |
| Database schema + migrations (golang-migrate, roles, grants) | 3 | Docker Compose for PostgreSQL |
| File store implementation (LocalFileStore) | 1 | Data models |

### Phase 1b: Ingestion Pipeline (13 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| IMAP adapter (connect, fetch, parse MIME) | 5 | Data models |
| LLM extraction (Pydantic-AI + Anthropic, prompt engineering) | 5 | Data models |
| PDF renderer (weasyprint, HTML/text/attachment handling) | 3 | None |

### Phase 1c: CLI and Search (5 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| CLI commands (ingest, search, show) | 3 | All pipeline components |
| Search queries (vendor substring, amount range, date range) | 2 | Database layer |

### Phase 1d: Integration and Polish (5 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| End-to-end integration testing | 3 | All components |
| Error handling, logging, dry-run mode | 2 | All components |

**Total: 31 story points**

### Critical Path

```
Scaffolding → Data Models → DB Schema + Migrations ─┐
                    │                                ├→ CLI + Search → Integration Testing
                    ├→ IMAP Adapter ─────────────────┤
                    ├→ LLM Extraction ───────────────┤
                    └→ PDF Renderer ─────────────────┘
```

Data models are the shared dependency. IMAP adapter, LLM extraction, and PDF renderer can be developed in parallel once models are defined.

---

## Testing Strategy

### Unit Tests

| Component | Test Focus |
|-----------|------------|
| `models.py` | Pydantic validation, serialization, edge cases (negative amounts, missing fields) |
| `extraction.py` | Mock LLM responses, validate parsing of various receipt formats |
| `renderer.py` | HTML-to-PDF rendering with mock email content |
| `store.py` | File naming, slugification, directory creation, duplicate handling |
| `db.py` | Query building, parameter binding (against mock/in-memory where possible) |
| `adapters/imap.py` | MIME parsing with fixture emails (mock IMAP connection) |

### Integration Tests

| Scenario | What It Tests |
|----------|---------------|
| IMAP fetch + extract + store | Full pipeline with a test IMAP server or recorded messages |
| Search with various filter combinations | Parameterized SQL queries against seeded test data in `receipt.receipts` |
| Idempotent re-ingestion | Running ingest twice produces no duplicates (`uq_receipts_source_id` enforced) |
| PDF rendition quality | Render sample HTML/text/attachment emails, verify PDF is valid |
| Migration up/down | Verify golang-migrate scripts apply and roll back cleanly |

### Test Data

- Fixture emails covering: HTML order confirmation, forwarded receipt, PDF attachment, plain-text receipt, multi-item order
- Seed database with known receipts for search testing

### Coverage Target

80% minimum per Python standards. Critical paths (extraction, idempotency, search) targeted at 90%+.

---

## Deployment

### Local Development

```bash
make setup                    # Install deps, pre-commit hooks
docker compose up -d          # Start PostgreSQL 18
make migrate-up               # Run migrations (public → receipt)
cp example.env .env           # Configure credentials
receipt-index ingest          # Run ingestion
receipt-index search --vendor amazon
```

### Docker Compose

```yaml
services:
  db:
    image: postgres:18
    environment:
      POSTGRES_DB: receipt_index_dev
      POSTGRES_USER: receipt_index
      POSTGRES_PASSWORD: localpass
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

Application runs directly on the host (not containerized) for Phase 1 — this is a local CLI tool, not a deployed service. The application connects as `receipt_index_dev_write`; migrations run as `receipt_index_dev_all`.

---

## Risks & Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| LLM extraction accuracy too low for some receipt formats | Medium | Medium | Log confidence scores, flag low-confidence extractions, iterate on prompts. Manual override not in scope but database can be edited directly. |
| `weasyprint` struggles with complex email HTML | Medium | Medium | Email HTML is often poorly structured. Test with real receipts early. Fall back to simpler HTML stripping + re-rendering if needed. |
| IMAP connection reliability | Low | Low | Standard retry logic. Ingestion is one-shot, can be re-run. Idempotency ensures safety. |
| Anthropic API rate limits or outages | Low | Low | Process receipts sequentially with modest delay. Idempotency allows resuming after partial failure. |
| PDF attachments that are scanned images (not text) | Medium | Medium | Phase 1 scope: pass image-based PDFs through without text extraction. Phase 2 (Google Drive + OCR) will address this more fully. For Phase 1, the email subject/sender may still provide enough metadata. |

---

## Out of Scope (Phase 1)

- Google Drive ingestion (Phase 2)
- Duplicate receipt detection across sources (RD-07)
- Web UI
- Automated accounting software matching
- Receipt content full-text search (only structured metadata search)
- Multi-currency support beyond storing the currency code
