# Technical Design: Phase 2 — Google Drive Integration & Config-Based Sources

**Version:** 0.1 — Draft
**Date:** 2026-03-14
**Product Spec:** [receipt-search-index-spec.md](../../product/features/receipt-search-index-spec.md)

---

## Context

Phase 1 delivered email receipt ingestion from a single IMAP source configured via environment variables. Phase 2 expands the system in two ways:

1. **Config-based source definitions** — Replace per-source environment variables with a YAML configuration file that supports multiple named sources of any type. Secrets remain in environment variables.
2. **Google Drive ingestion** — Add a new source adapter for Google Drive folders containing scanned receipts and print-to-PDF captures. Drive files require vision-based metadata extraction (no email envelope to assist).

### Key Technical Challenges

1. **Config migration** — Moving from flat env vars to structured YAML config without breaking existing users or complicating the simple case.
2. **Google Drive OAuth2** — OAuth requires a one-time browser-based authorization flow to obtain a refresh token. Subsequent runs use the refresh token (stored as env var) for non-interactive access.
3. **Vision-based extraction** — Drive-sourced receipts (especially scanned images) have no email subject/sender context. The LLM must extract metadata purely from document visual content.
4. **Model generalization** — `RawReceipt` and the pipeline are email-centric. They need to become source-agnostic without breaking the working IMAP flow.

### Architectural Decisions

- [ADR-0006: Config-Based Source Definitions](../adr/0006-config-based-source-definitions.md)

### Standards

- [Python Standards](https://github.com/rmorison/engineering-standards/blob/main/code/python-standards.md)
- [Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md)

---

## Approach

### Solution Overview

Three workstreams that can be partially parallelized:

1. **Config system** — YAML-based source definitions with Pydantic validation, env var interpolation, and backward compatibility.
2. **Model & pipeline generalization** — Make `RawReceipt`, the pipeline, and the DB schema source-agnostic. Add source name tracking.
3. **Google Drive adapter** — OAuth2 auth, Drive API file listing/download, image-to-PDF conversion, vision-based extraction.

### Alternatives Considered

- **TOML config**: Python-native (PEP 680), but less readable for nested source definitions with varying schemas per type. YAML's per-document typing maps more naturally to polymorphic source configs.
- **Config in database**: Over-engineered for a CLI tool. Config should be version-controllable.
- **Service account for Drive**: Simpler auth flow but requires sharing folders with the service account — unnatural for personal Drive usage. OAuth with offline refresh token is a better fit.

---

## Design

### 1. Configuration System

#### Config File Location

Search order (first found wins):
1. `--config` CLI flag
2. `RECEIPT_INDEX_CONFIG` environment variable
3. `./receipt-index.yaml` (project-local)
4. `~/.config/receipt-index/config.yaml` (XDG convention)

#### Config Schema

```yaml
# receipt-index.yaml
sources:
  - name: personal-email
    type: imap
    host: mail.example.com
    port: 993
    username: user@example.com
    password: ${IMAP_PASSWORD}           # env var interpolation
    folder: INBOX.Receipts
    use_ssl: true                        # default: true

  - name: scanned-receipts
    type: gdrive
    folder_id: "1aBcDeFgHiJkLmNoPqRsTuVwXyZ"
    credentials_json: ${GDRIVE_CREDENTIALS_JSON}   # OAuth client config
    token_json: ${GDRIVE_TOKEN_JSON}               # OAuth refresh token

database:
  url: ${DATABASE_URL}

store:
  path: ./data/receipts

llm:
  model: claude-haiku-4-5-20251001       # default
  api_key: ${ANTHROPIC_API_KEY}

logging:
  level: INFO                            # default
```

#### Config Data Model

```python
# config.py additions

class SourceConfig(BaseModel):
    """Base for all source configurations."""
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: Literal["imap", "gdrive"]

class ImapSourceConfig(SourceConfig):
    type: Literal["imap"] = "imap"
    host: str
    port: int = 993
    username: str
    password: str
    folder: str = "INBOX"
    use_ssl: bool = True

class GdriveSourceConfig(SourceConfig):
    type: Literal["gdrive"] = "gdrive"
    folder_id: str
    credentials_json: str    # OAuth client credentials JSON blob (content of client_secret.json)
    token_json: str          # OAuth token JSON with refresh token

class DatabaseConfig(BaseModel):
    url: str

class StoreConfig(BaseModel):
    path: str = "./data/receipts"

class LlmConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    api_key: str

class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

class AppConfig(BaseModel):
    sources: list[ImapSourceConfig | GdriveSourceConfig]
    database: DatabaseConfig
    store: StoreConfig
    llm: LlmConfig
    logging: LoggingConfig = LoggingConfig()
```

#### Env Var Interpolation

Use `${VAR_NAME}` syntax in YAML values. Process during YAML loading before Pydantic validation:

```python
import re, os

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

def _interpolate_env(value: str) -> str:
    def _replace(match: re.Match) -> str:
        var = match.group(1)
        val = os.environ.get(var)
        if val is None:
            raise ValueError(f"Environment variable {var!r} not set")
        return val
    return _ENV_PATTERN.sub(_replace, value)
```

Walk the parsed YAML dict and apply `_interpolate_env` to all string values before passing to Pydantic. The implementation should collect all missing env var names across the entire config and raise a single error listing them all, so users can fix everything in one pass rather than one error per run.

#### No Backward Compatibility

Phase 2 replaces the Phase 1 env var configuration entirely. The old `get_imap_config()`, `get_database_url()`, and related helpers are removed. If no config file is found, the CLI fails with a clear error and example config. This is a clean break — no fallback to env vars, no deprecation period.

### 2. Model & Pipeline Generalization

#### RawReceipt Changes

Add optional file-oriented fields; make email fields optional:

```python
@dataclass
class RawReceipt:
    source_id: str
    source_name: str                      # NEW: config source name
    source_type: str                      # NEW: "imap" or "gdrive"
    date: datetime
    subject: str = ""                     # Was required; now defaults empty
    sender: str = ""                      # Was required; now defaults empty
    html_body: str | None = None
    text_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    file_name: str | None = None          # NEW: original filename (Drive)
    file_content: bytes | None = None     # NEW: raw file bytes (Drive)
    file_content_type: str | None = None  # NEW: MIME type (Drive)
```

The IMAP adapter continues populating `subject`, `sender`, `html_body`, `text_body`, and `attachments`. The Drive adapter populates `file_name`, `file_content`, and `file_content_type`.

#### Receipt Model Changes

Update the `Receipt` Pydantic model to include the new DB columns. Since `insert_receipt` uses `RETURNING *` → `Receipt.model_validate(row)`, the model must match the schema:

```python
class Receipt(BaseModel):
    # ... existing fields ...
    source_name: str                       # NEW: NOT NULL after migration backfill
    file_name: str | None = None          # NEW: original filename (Drive)
```

#### Pipeline Changes

Update `run_ingest` to:
- Read `source_name` and `source_type` from each `RawReceipt` (instead of hardcoding `"imap"`)
- Pass these through to `insert_receipt`
- Route to the appropriate extraction path based on `raw.source_type`
- Filter `get_processed_source_ids()` by source name for efficient idempotency checks

The `run_ingest` signature is unchanged — `source_name` and `source_type` live on `RawReceipt`, not the pipeline function.

#### Extraction Changes

Add a document extraction path alongside the email extraction path:

```python
def extract_metadata(
    raw: RawReceipt,
    *,
    agent: Agent | None = None,
) -> ReceiptMetadata:
    if raw.source_type == "gdrive":
        return _extract_from_document(raw, agent=agent)
    return _extract_from_email(raw, agent=agent)
```

For Drive-sourced documents:
- **PDF files**: Extract text via `pdf_reader.py` (existing). If text extraction yields little content, fall back to vision (render pages as images, send to LLM).
- **Image files**: Send directly to the LLM as image input via Pydantic-AI's multimodal support.
- **System prompt**: Adapted version without email-specific context; focuses on extracting vendor, amount, date from document content.

```python
_DOCUMENT_SYSTEM_PROMPT = """\
You are a receipt metadata extractor. Given a receipt document (scanned image \
or PDF), extract the following fields:

- vendor: The business name on the receipt
- amount: The total amount charged (numeric, e.g. 42.99)
- currency: ISO 4217 currency code (e.g. "USD")
- date: The transaction date (YYYY-MM-DD)
- description: Brief summary of what was purchased (optional)
- confidence: Your confidence in the extraction from 0.0 to 1.0. \
Use below 0.5 if this does not appear to be a receipt or key fields are unclear.

For multi-item receipts, use the total amount. If the currency is not stated, \
assume USD.\
"""
```

#### Renderer Changes

For Drive-sourced files:
- **PDF files**: Already a PDF — no rendering needed. Copy directly.
- **Image files**: Convert to PDF using Pillow (`Image.save(format="PDF")`).

Update `render_pdf` to handle the `source_type`:

```python
def render_pdf(raw: RawReceipt) -> bytes:
    if raw.source_type == "gdrive":
        return _render_drive_file(raw)
    return _render_email(raw)  # existing logic
```

#### Database Migration

Add nullable columns to `receipt.receipts` for source name and file metadata:

```sql
-- 000003_add_source_name_and_file_columns.up.sql

ALTER TABLE receipt.receipts
    ADD COLUMN source_name TEXT,
    ADD COLUMN file_name TEXT;

-- Backfill existing rows with a placeholder name (not a source type)
UPDATE receipt.receipts SET source_name = 'legacy-imap' WHERE source_name IS NULL;

-- Now enforce NOT NULL — all rows have a value
ALTER TABLE receipt.receipts ALTER COLUMN source_name SET NOT NULL;

-- Grant write access to new columns
GRANT INSERT (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT UPDATE (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_write;

-- Grant read access to new columns
GRANT SELECT (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_read;
```

```sql
-- 000003_add_source_name_and_file_columns.down.sql

ALTER TABLE receipt.receipts
    DROP COLUMN IF EXISTS source_name,
    DROP COLUMN IF EXISTS file_name;
```

#### Repository Changes

Update `insert_receipt()` to accept the new columns:

```python
def insert_receipt(
    conn: ...,
    *,
    source_id: str,
    source_type: str,
    source_name: str,              # NEW
    vendor: str,
    amount: Decimal,
    currency: str,
    receipt_date: date,
    description: str | None,
    confidence: float,
    pdf_path: str,
    email_subject: str | None,
    email_sender: str | None,
    email_date: datetime | None,
    file_name: str | None = None,  # NEW
) -> Receipt:
```

Update `get_processed_source_ids()` to accept an optional `source_name` filter so each source only checks its own processed IDs:

```python
def get_processed_source_ids(
    conn: ...,
    source_name: str | None = None,
) -> set[str]:
    if source_name:
        cur = conn.execute(
            "SELECT source_id FROM receipt.receipts WHERE source_name = %(source_name)s",
            {"source_name": source_name},
        )
    else:
        cur = conn.execute("SELECT source_id FROM receipt.receipts")
    return {str(row["source_id"]) for row in cur.fetchall()}
```

Update `search_receipts()` with an optional `source_name` filter (required for FR-17 and `--source` CLI flag):

```python
def search_receipts(
    conn: ...,
    *,
    vendor: str | None = None,
    amount: Decimal | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_name: str | None = None,   # NEW
) -> list[Receipt]:
```

When `source_name` is provided, add `WHERE source_name = %(source_name)s` to the query.

Update `insert_receipt()` call in the pipeline to set `email_date` correctly per source type — Drive receipts have no email date:

```python
receipt = insert_receipt(
    conn,
    source_id=raw.source_id,
    source_type=raw.source_type,
    source_name=raw.source_name,
    # ...
    email_subject=raw.subject or None,
    email_sender=raw.sender or None,
    email_date=raw.date if raw.source_type == "imap" else None,
    file_name=raw.file_name,
)
```

### 3. Google Drive Adapter

#### OAuth2 Flow

Google Drive API requires OAuth2 for accessing a user's personal Drive. The flow:

1. **One-time setup** (`receipt-index auth gdrive`):
   - User provides OAuth client credentials (from Google Cloud Console)
   - CLI opens browser for authorization
   - User grants read-only Drive access
   - CLI receives authorization code, exchanges for refresh token
   - Refresh token displayed for user to store as env var (`GDRIVE_TOKEN_JSON`)

2. **Runtime**:
   - Adapter reads `credentials_json` and `token_json` from config (via env vars)
   - Uses `google-auth` library to refresh the access token
   - Calls Drive API with refreshed credentials

#### Adapter Implementation

```python
class GdriveAdapter:
    """Fetch unprocessed receipt files from a Google Drive folder."""

    def __init__(self, config: GdriveSourceConfig) -> None:
        self.config = config

    def fetch_unprocessed(self, processed_ids: set[str]) -> Iterator[RawReceipt]:
        service = self._build_service()
        files = self._list_files(service)
        for file_meta in files:
            file_id = file_meta["id"]
            if file_id in processed_ids:
                continue
            content, mime_type = self._download_file(service, file_meta)
            yield RawReceipt(
                source_id=file_id,
                source_name=self.config.name,
                source_type="gdrive",
                date=_parse_drive_date(file_meta["modifiedTime"]),
                file_name=file_meta["name"],
                file_content=content,
                file_content_type=mime_type,
            )
```

#### File Types Supported

| Drive MIME Type | Handling |
|----------------|----------|
| `application/pdf` | Download bytes directly |
| `image/jpeg`, `image/png` | Download, convert to PDF via Pillow |
| `application/vnd.google-apps.document` | Export as PDF via Drive API |
| Other | Skip with warning log |

#### Dependencies

New runtime dependencies for Phase 2:

| Package | Purpose |
|---------|---------|
| `pyyaml` | YAML config file parsing |
| `google-auth` | OAuth2 credential management |
| `google-api-python-client` | Google Drive API client |
| `Pillow` | Image-to-PDF conversion |

### 4. CLI Changes

#### `ingest` Command

Add `--source` and `--config` flags:

```
receipt-index ingest [--source NAME] [--config PATH] [--dry-run] [--limit N]
```

- `--source NAME`: Ingest from a specific named source only. If omitted, ingest from all configured sources.
- `--config PATH`: Override config file location.

When ingesting from multiple sources, process them sequentially and aggregate results:

```
$ receipt-index ingest
Source personal-email: Processed: 5  Skipped: 0  Failed: 0
Source scanned-receipts: Processed: 3  Skipped: 1  Failed: 0
Total: Processed: 8  Skipped: 1  Failed: 0
```

#### `auth` Command (new)

```
receipt-index auth gdrive --client-credentials PATH
```

Runs the one-time OAuth2 authorization flow and outputs the token JSON for the user to store as an environment variable.

#### `search` Command

Add `--source` filter:

```
receipt-index search [--source NAME] [--vendor TEXT] ...
```

Filters results to receipts from the named source.

---

## Data Flow

### Drive Ingestion Pipeline

```
1. CLI loads config, resolves source(s) to ingest
2. For each source:
   a. Instantiate adapter (ImapAdapter or GdriveAdapter)
   b. Load processed source_ids from DB (filtered by source_name)
   c. Adapter fetches unprocessed items
   d. For each RawReceipt:
      - Route to appropriate extraction (email or document)
      - Extract metadata via LLM → ReceiptMetadata
      - Render/convert to PDF → bytes
      - Save PDF to file store → relative_path
      - Insert into receipt.receipts with source_name
   e. Report per-source summary
3. Report aggregate summary
```

### Vision Extraction Flow (Drive Documents)

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐
│  Image   │───→│ Send as      │───→│  LLM (Haiku) │
│  File    │    │ image input  │    │  Vision mode │
└──────────┘    └──────────────┘    └──────┬───────┘
                                          │
┌──────────┐    ┌──────────────┐          ▼
│  PDF     │───→│ Extract text │───→ ┌──────────────┐
│  File    │    │ (pdfplumber) │    │  LLM (Haiku)  │
└──────────┘    └──────────────┘    │  Text mode    │
                                    └──────┬───────┘
                                           │
                                           ▼
                                    ┌──────────────┐
                                    │ReceiptMetadata│
                                    └──────────────┘
```

If PDF text extraction yields insufficient content (<20 non-whitespace chars, matching the existing `pdf_reader.py` threshold), fall back to rendering the first page as an image and using vision mode.

---

## Implementation Plan

### Phase 2a: Config System (9 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| Config data model (Pydantic models, YAML loading, env var interpolation) | 3 | None |
| Config file discovery and loading | 2 | Config data model |
| Update CLI to use config system (`--config`, `--source` flags) | 3 | Config loading |
| Migration guide for existing env var users + example config file | 1 | Config loading |

### Phase 2b: Model & Pipeline Generalization (5 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| Generalize `RawReceipt` and update IMAP adapter | 2 | None |
| Update pipeline and repository for source-aware operation, multi-source orchestration | 2 | Generalized models |
| DB migration: add `source_name`, `file_name` columns (up + down) | 1 | None |

### Phase 2c: Google Drive Adapter (10 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| OAuth2 auth flow and `auth gdrive` CLI command | 3 | Config system |
| Drive adapter: file listing, download, MIME handling | 5 | Generalized models, config |
| Image-to-PDF conversion (Pillow) | 2 | None |

### Phase 2d: Vision Extraction & Integration (8 points)

| Task | Points | Dependencies |
|------|--------|--------------|
| Document extraction path (vision + text-based) in `extraction.py` | 5 | Drive adapter |
| Integration testing (mock Drive API + Postgres) | 3 | All components |

**Total: 32 story points**

### Critical Path

```
Config Data Model → Config Loading → CLI Updates ─────────────────────┐
                                                                      │
Generalize Models → Update Pipeline ──────────────────────────────────┤
                                                                      ├→ Integration Testing
DB Migration ─────────────────────────────────────────────────────────┤
                                                                      │
OAuth2 Flow → Drive Adapter → Document Extraction ────────────────────┘
                    │
Image-to-PDF ───────┘
```

Config data model and model generalization can proceed in parallel. Drive adapter depends on both config and generalized models. Integration testing is the final gate.

---

## Testing Strategy

### Unit Tests

| Component | Test Focus |
|-----------|------------|
| Config loading | YAML parsing, env var interpolation, Pydantic validation, clear error on missing config file |
| Config models | Pydantic validation for all source types, edge cases |
| `GdriveAdapter` | Mock Drive API responses, file type handling, date parsing |
| Document extraction | Mock LLM responses, vision vs. text routing, prompt construction |
| Image-to-PDF | Pillow conversion, valid PDF output |
| Pipeline | Multi-source orchestration, per-source result aggregation |

### Integration Tests

| Scenario | What It Tests |
|----------|---------------|
| Config → IMAP ingest | Config-based IMAP source works end-to-end (replaces env var flow) |
| Config → Drive ingest | Drive adapter with mock API, through extraction and storage |
| Multi-source ingest | Two sources in config, both ingested, results aggregated |
| Idempotent Drive re-ingest | Re-running skips already-processed Drive file IDs |
| `--source` filtering | Only specified source is ingested; search filters by source |
| Migration up/down | New columns added/removed cleanly |

### Test Data

- Sample Drive API response fixtures (file listing, file download)
- Test images (scanned receipt JPG/PNG) for vision extraction
- Test PDFs for text extraction path

---

## Risks & Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| OAuth2 token refresh complexity | Medium | Medium | Use `google-auth` library which handles refresh automatically. Provide clear setup docs. |
| Vision extraction accuracy on scanned receipts | Medium | Medium | Haiku supports vision; test with real scans early. Fall back to text extraction for text-based PDFs. |
| Config migration friction | Low | Low | Clean break (no env var fallback). Provide clear error message with example config and a migration guide. |
| Google API quota limits | Low | Low | Drive API has generous quotas (1B queries/day). Not a concern for personal use. |
| Pillow dependency weight | Low | Low | Pillow is widely used and well-maintained. Only needed for image→PDF conversion. |

---

## Out of Scope (Phase 2)

- Handwritten receipt recognition (Haiku vision handles printed/typed text on scanned receipts; handwritten notes and manual tallies are unreliable and excluded)
- Real-time Drive folder watching (one-shot ingestion per product spec RD-06)
- Cloud storage backend for file store (future)
- Duplicate receipt detection across sources (RD-07)
- Web UI
