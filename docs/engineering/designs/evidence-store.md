# Technical Design: Evidence Store (Google Drive, Originals, Hashes, Provenance)

**Version:** 0.1 (Draft)
**Date:** 2026-10-26
**Product Spec:** [receipt-search-index-spec.md](../../product/features/receipt-search-index-spec.md)

---

## Context

The service keeps one file per receipt: a rendered PDF, written to the local disk under `YYYY/MM/{date}__{vendor}__{amount}.pdf`. `receipts.pdf_path` records its relative path, and an `rclone copy` mirrors the tree to Dropbox. The main consumer is an agent that builds claim packets and reconciliation packets from receipts. It needs three things the store doesn't give it:

1. **Originals.** For a claim or an audit, the evidence is the original email (its headers, sender and date) or the original scan, not the convenience PDF. Today the original email, its attachments and the Drive source file are all thrown away after rendering.
2. **Files that can be verified.** Nothing records what a file should contain, so a damaged or swapped file goes unnoticed, and a file can't be matched against an external archive without keeping a second copy of it.
3. **No double-counting.** The same receipt arriving as an email, as a forward and as a scan becomes three receipts, because each source item is tracked only by its own ID. RD-07 put duplicate detection out of scope, and claim totals pay for that.

There are smaller problems too. `FileStore.get_path` returns a local `Path`, so no remote backend fits the protocol. The CLI hardcodes `LocalFileStore`. Some vendor PDFs need their headers repaired before they can be merged into a packet, and that failure only shows up when a packet is being built.

### Key Technical Challenges

1. **Drive access without over-reach.** Ingest reads, and then archives, files the operator drops into a Drive folder that the app didn't create. That rules out `drive.file` and needs the full `drive` scope.
2. **Write-once on a store whose owner can change anything.** Drive has no immutable object mode.
3. **Duplicate detection that never merges two real purchases.** Two identical parking charges on one day are two expenses. A shared terms-and-conditions PDF doesn't make two receipts one.
4. **Moving the live store without losing a file**, with every moved file verified.

### Architectural Decisions

- [ADR-0009: Google Drive evidence store with originals and content hashes](../adr/0009-google-drive-evidence-store-with-originals-and-hashes.md) (amends [ADR-0003](../adr/0003-local-file-store-with-abstraction.md))
- [ADR-0010: Provenance records and duplicate detection](../adr/0010-provenance-records-and-duplicate-detection.md) (amends RD-07 in the product spec)

### Standards

- [Python Standards](https://github.com/rmorison/engineering-standards/blob/main/code/python-standards.md)
- [Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md)

### Related Designs

- **[Always-on ingest](always-on-ingest.md)** calls this store for every item it ingests and archives Drive dropoff files into it. The interface between the two is in [Write Path](#7-write-path).
- **Receipt data model** owns the receipt's own fields (paid amount, line items, payment, order number, refunds, tags, external links, extraction history). This design owns files, storage references and provenance. The split is in [Schema Split](#schema-split-with-the-receipt-data-model-design).

---

## Approach

### Solution Overview

Google Drive becomes the evidence store, behind a storage interface that has no local paths in it. A dedicated Google account owns the store and holds the full `drive` scope. The operator shares only the dropoff folder with that account, so its token can't reach the rest of the operator's Drive.

For every source item, the store keeps the original exactly as received: the raw RFC 822 message and each of its attachments, or the Drive source file. It also keeps a rendered PDF, as today. Every stored file has a sha256 in the database. Verification reads Drive's own checksum metadata, so routine checks download nothing.

A receipt gains one or more **provenance records**, one per source item that contributed to it. A new item that matches an existing receipt by the hash of its whole original, by the hash of its receipt document, or by order number joins that receipt instead of creating a new one. Matches on vendor, amount and date alone are flagged for review and never linked. A merge never deletes a receipt: the receipt it empties is marked as merged into the one that remains, so its history and its rendition are kept. Links can be undone, and every link and unlink is recorded.

The local backend stays for devtest, tests and offline use. Existing files move to Drive with a resumable, verified migration. A second copy of the Drive store (a replica or a cold archive) is deferred to a future feature, and the gap is recorded under [Risks](#risks--mitigation).

### Alternatives Considered

| Option | Why not |
|--------|---------|
| **Local disk stays primary, Drive only as a mirror** | Consumers need a stable reference they can open from anywhere. A mirror has no identity of its own, and the local disk on one server stays the single point of failure for originals. |
| **S3 or GCS** (what ADR-0003 expected) | It adds a cloud account, a bill and credentials, for a single-operator system at under a gigabyte a year. It isn't browsable by the operator or a bookkeeper without tooling. Drive already holds the scans and already has OAuth wired in. |
| **The operator's own account with `drive.file`** | `drive.file` sees only files the app created or that were opened through the Picker. It can't read, list in the changes feed, or move files the operator uploads to the dropoff folder. |
| **The operator's own account with the full `drive` scope** | It works, but the token can read and change the operator's whole Drive. A leaked token or a bug would reach far beyond receipts. |
| **A service account with shared folders** | Sharing would confine its access, but on a personal Google account a service account has no storage quota, so it can't own the evidence files. Shared drives need Workspace. RD-09 already turned service accounts down. |
| **Database BLOBs** | Rejected in ADR-0003 for browsability and backups, and the reasons still hold. |

---

## Design

### 1. Storage Interface

The `FileStore` protocol (save, `get_path`, exists) is replaced by an `EvidenceStore` protocol that works in bytes and opaque references:

```python
@dataclass(frozen=True)
class StoredFile:
    id: UUID                # receipt.stored_files.id
    sha256: bytes | None    # None only for native Google Docs
    byte_size: int | None
    media_type: str
    ref: str                # "drive:<fileId>" or "local:<relative/path>"


class EvidenceStore(Protocol):
    def put(self, data: bytes, *, media_type: str, purpose: Purpose,
            folder: StoreFolder, name: str) -> StoredFile: ...
    def open(self, ref: str) -> BinaryIO: ...          # streamed read
    def exists(self, ref: str) -> bool: ...
    def stat(self, ref: str) -> FileStat: ...          # size + backend checksum
    def rename(self, ref: str, name: str) -> None: ... # renditions only
```

- **Storing an original is content-addressed and idempotent.** The bytes are hashed first. If `stored_files` already holds that sha256 with an `original` location **that the store account owns** (`file_locations.owner = 'store'`), the existing file is returned and nothing is written, so a retry after a crash never stores an original twice. `put` itself touches no database: the lookup sits in the helpers above it (`store_email_original`), and the rows are recorded in the item's own transaction (ticket 3). A location the operator's account owns (an adopted dropoff file, under the move default) doesn't count, because the operator can delete it: an email attachment with the same bytes still gets its own store-owned copy.
- **Concurrent writers.** `serve` and `backfill` can store the same bytes at the same moment. `stored_files` rows are inserted with `INSERT ... ON CONFLICT (sha256) DO NOTHING`, followed by a `SELECT` by sha256, and `file_locations` rows with `ON CONFLICT (backend, locator) DO NOTHING`, so the loser of the race reuses the winner's row. A Drive upload that lost the race is trashed (it is the process's own unrecorded upload, which the guard allows).
- **Each receipt has its own rendition location.** The `stored_files` row is shared when two receipts' renditions have the same bytes (a PDF passed through as the rendition, a scan dropped twice). But every receipt gets its own rendition file in `renditions/`, recorded as a `file_locations` row with that `receipt_id`. A rename then touches one receipt's file only. A retry finds the receipt's existing rendition location and writes nothing.
- **There is no update or delete for originals.** `rename` refuses any location whose purpose is `original`. Only renditions get renamed, when re-extraction changes the vendor, date or amount in the name.
- **`open` streams.** Callers never get a local path: the CLI's `show`, the read-only MCP server and packet builders all read through `open(ref)`.
- **Two backends.** `DriveEvidenceStore` is the production backend. `LocalEvidenceStore` keeps today's local tree (same layout, with the folders below) for devtest, tests and offline use, so NFR-01 still holds.
- **The backend is chosen in config**, not hardcoded in the CLI:

```yaml
store:
  backend: drive                 # or: local
  path: /app/data/receipts       # local backend root; also where migration reads from
  drive:
    root_folder_id: ${RECEIPT_STORE_ROOT_FOLDER_ID}
    token_json: ${GDRIVE_STORE_TOKEN_JSON}
```

Higher-level helpers sit on top of `put` so that ingest never deals with attachments itself. They are given in [Write Path](#7-write-path).

### 2. What Is Stored

| Purpose | What | When |
|---------|------|------|
| `original` | IMAP: the full RFC 822 message exactly as fetched (`message.eml`) | Every ingested email |
| `original` | IMAP: each MIME attachment, decoded, as its own file | Every ingested email with attachments |
| `original` | Drive: the dropoff file itself, adopted into the store | Every ingested Drive file |
| `original` | Drive, native Google Doc: the Doc itself (no bytes to hash) and a PDF export snapshot taken at adoption, with its own hash | Every ingested native Doc |
| `rendition` | The rendered PDF (today's file) | Every receipt |

**Originals are write-once.** That is enforced in three layers, and the design is honest that none of them is tamper-proofing against the account owner:

1. The interface has no way to change or delete an original.
2. The Drive backend sets `contentRestrictions.readOnly` on every original, with a reason naming the store. The owner can lift it by hand, but nothing lifts it by accident.
3. Verification ([section 3](#3-hashes-and-verification)) detects any change.

**Native Google Docs** have no bytes to hash. The Doc itself is the original: `provenance.original_file_id` points at its `drive_native` row (no sha256). The PDF export snapshot taken at adoption is a second `provenance_files` row with role `drive_export`. Wherever this design compares or names an original by hash (rule 1, the folder's hash prefix), it uses the export's sha256. Drive's export is limited to 10 MB. If the export fails (too large, or an API error), the adopt fails and the item goes to the ingest design's retries. An export that is too large is terminal on the first attempt, and the item moves to `needs-attention` with that reason, since retrying can't help.

**The receipt document.** Each provenance record names the one file the receipt was read from: its *receipt document*. It is `certain` only when the source makes it so, or the extraction says so:

- **Drive source:** the source file (for a native Doc, its export snapshot): `certain`, once it is adopted. Until then (`archive: false`, original `pending`) there is no stored file to name, so the receipt document and its certainty are NULL.
- **Email:** the attachment the extraction reports it read the receipt from. The receipt data-model design's extraction output reports this as `receipt_document`: an attachment file name, `"body"`, or null. The store maps a file name to the `provenance_files` row with that raw MIME file name. It is `certain` only when the name matches exactly one attachment. A name the extractor had to clean for its prompt (control characters, a run of three or more dashes, over 255 characters) matches none. A part with no MIME file name (an inline image the adapter names by its Content-ID, or `unnamed`) has a NULL `file_name` and matches none. A name shared by two attachments (the first of them), a name that matches none, or null (the first PDF, which the renderer passes through) leaves it `ambiguous`; a null with no PDF, or `"body"`, means no receipt document. So a lone PDF the extraction doesn't name is `ambiguous`, because a vendor that puts the receipt in the body often attaches only its terms and conditions.
- **Email whose receipt is the HTML or text body:** no receipt document.
- **Image attachments** (logos, inline images, signatures) are the receipt document only when the extraction names one, as for a photographed or screenshot receipt.

An `ambiguous` receipt document takes no part in hash linking ([section 8](#8-provenance-and-duplicates)).

### 3. Hashes and Verification

- **sha256 of the exact stored bytes**, computed before upload, for every original, attachment, export and rendition, stored as `bytea` in `receipt.stored_files`.
- **After every upload**, the Drive backend reads the new file's `sha256Checksum` (Drive computes it for every binary file) and compares it with the local hash. A mismatch fails the `put`. The file is trashed and the item retried, so no unverified file is ever recorded.
- **`receipt-index verify`** checks each location:
  - The default mode reads Drive metadata only (`sha256Checksum`, size, `trashed`, parents) and compares it with the database. It is cheap enough to run daily.
  - `--deep` downloads and re-hashes. It is run monthly, or after an incident.
  - `--orphans` lists Drive files under the store root with no database row, and rows with no Drive file.
  - Every check records `last_verified_at` on the location and reports mismatches, missing files and unexpected read-only restrictions that have been lifted.
- **External archives** can be matched by hash: a claim or audit archive holding a copy of an original can be checked against the database without the store keeping a second copy.

### 4. Drive Account, Scope and Guard

**Account.** A dedicated Google account is created for the receipt store. It owns the evidence root folder, and every file the service uploads counts against its quota. The volume is well under a gigabyte a year, so the free tier is enough.

**Scope.** The full `drive` scope, held only by the dedicated account. `drive` is a restricted scope, like the `drive.readonly` in use today, so the OAuth app's verification class doesn't change.

**Setup, once** (written up in the server-deployment runbook by the Drive backend ticket):

1. Create the dedicated account and turn on two-step verification. Its recovery goes to the operator.
2. Check the OAuth consent screen's publishing status. It must be **In production**, not **Testing**: in Testing, Google expires refresh tokens after seven days, which would stop ingest within a week. An unverified app in production shows a warning screen at consent and is limited to a small number of users, which is fine for one account.
3. As the dedicated account, create the evidence root folder (`receipt-index/`). Share it with the operator's own account as **Viewer**, so the operator and a bookkeeper can browse it without holding the token.
4. As the operator, share each Drive dropoff folder with the dedicated account as **Editor**.
5. Run `receipt-index auth gdrive` signed in as the dedicated account, requesting `https://www.googleapis.com/auth/drive`. Store the token as `GDRIVE_STORE_TOKEN_JSON`. Every Drive source (`gdrive` sources in the config) and the store use this one token, and the operator's `drive.readonly` token is retired. This is the single re-authorization both designs need.

**Guard.** The full scope reaches everything the dedicated account owns or has been shared. All Drive calls go through one client wrapper (`drive_client.py`), which refuses any call outside these rules:

| Operation | Allowed on |
|-----------|-----------|
| Read, list, changes feed | The evidence root and its descendants, and each configured dropoff folder and its descendants |
| Create, upload, rename, move | Inside the evidence root only |
| Move (parent change) | Out of a configured dropoff folder (or its `needs-attention/` subfolder) into the evidence root's `originals/` tree, or into `needs-attention/`. Nothing else |
| Copy | *Only under the copy fallback ([Open Questions](#open-questions) item 1):* from a configured dropoff folder into the evidence root's `originals/` tree |
| Move into the dropoff's `processed/` subfolder, and create that subfolder | *Only under the copy fallback:* out of a configured dropoff folder, after its copy is recorded |
| Set `contentRestrictions` | Originals inside the evidence root |
| Trash | Only a file this process uploaded that failed verification, in the same call |
| Delete (permanent), permission changes, ownership transfer | Never |

The two fallback rows are switched off unless the config selects the fallback (`store.drive.dropoff_archive: copy`; the default is `move`). The wrapper resolves each target's ancestry (cached per run) before acting. A refusal is an error, never a silent skip. Unit tests cover each row, and a test fails the build if any module other than the wrapper imports the Drive client library.

### 5. Folder Layout

This builds on today's `YYYY/MM/{date}__{vendor}__{amount}.pdf`:

```
receipt-index/                                  (evidence root, owned by the store account)
├── renditions/
│   └── 2026/
│       └── 10/
│           └── 2026-10-14__acme-parking-u-s-llc__12.00.pdf
└── originals/
    └── 2026/
        └── 10/
            └── 2026-10-14__mail-receipts__3f9a1c0b2e7d/
                ├── message.eml
                ├── 01__receipt.pdf
                └── 02__terms-and-conditions.pdf
```

- **Renditions** keep today's metadata name, so the tree stays browsable by period. A Drive file ID survives a rename, so when re-extraction changes the vendor or amount the file is renamed and no reference breaks. Name collisions get today's numeric suffix.
- **Originals** go in one folder per source item, named `{received-date}__{source-name}__{sha256[:12]}`. The year and month come from the date the item was received, not the receipt date, since the original's identity doesn't depend on extraction. The hash prefix is from the whole original: the `.eml`, or the Drive file (for a native Doc, its export). These folders and files are never renamed.
- **Attachment names are built, never taken from the sender.** A MIME filename is untrusted input (it can hold `../`, control characters or thousands of characters). The stored name is `{position:02d}__{slugified stem, at most 60 characters}.{extension}`, with the extension from an allow-list keyed on the media type (`pdf`, `jpg`, `png`, `gif`, `webp`, `html`, `txt`, otherwise `bin`). The raw filename is kept only in `provenance_files.file_name`. The position prefix also keeps two attachments with the same name apart. `LocalEvidenceStore` resolves every write and every `local:` reference under `store.path`, and refuses any path that resolves outside it.
- **Dropoff `needs-attention/`** (owned by the always-on ingest design's [dropoff archive](always-on-ingest.md#7-dropoff-archive)) is a subfolder of each dropoff folder. It holds skipped and terminally failed items, which aren't adopted.

The local backend uses the same layout under `store.path`, so a local tree and a Drive tree can be compared file for file.

### 6. Schema

All tables are in the `receipt` schema, with UUID v7 keys and named constraints, per the database standards. Grants go to the existing `_write` and `_read` roles, column by column for writes as in the earlier migrations. Every migration is mirrored in `_run_migrations()` in `tests/integration/conftest.py`.

```sql
-- One row per distinct content. The same bytes stored twice are one row.
CREATE TABLE receipt.stored_files (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    content_kind      TEXT        NOT NULL DEFAULT 'bytes',  -- 'bytes' | 'drive_native'
    sha256            BYTEA,
    byte_size         BIGINT,
    media_type        TEXT        NOT NULL,
    pdf_status        TEXT,                -- 'ok' | 'repaired' | 'unparseable'; NULL if not a PDF
    repaired_from_id  UUID,                -- a repaired rendition points at the PDF it came from
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_stored_files PRIMARY KEY (id),
    CONSTRAINT uq_stored_files_sha256 UNIQUE (sha256),
    CONSTRAINT ck_stored_files_content_kind CHECK (content_kind IN ('bytes', 'drive_native')),
    CONSTRAINT ck_stored_files_sha256_present
        CHECK (content_kind = 'drive_native' OR (sha256 IS NOT NULL AND octet_length(sha256) = 32)),
    CONSTRAINT ck_stored_files_pdf_status CHECK (pdf_status IN ('ok', 'repaired', 'unparseable')),
    CONSTRAINT fk_stored_files_repaired_from FOREIGN KEY (repaired_from_id)
        REFERENCES receipt.stored_files (id)
);

-- Where each stored file lives. A file can have several locations: an attachment that is
-- also a rendition has one under originals/ and one under renditions/ per receipt using it;
-- during migration a rendition has a local and a Drive location; a future second copy adds a backend.
CREATE TABLE receipt.file_locations (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    file_id           UUID        NOT NULL,
    backend           TEXT        NOT NULL,  -- 'drive' | 'local'
    locator           TEXT        NOT NULL,  -- Drive file ID, or path relative to the local root
    purpose           TEXT        NOT NULL,  -- 'original' | 'rendition'
    receipt_id        UUID,                  -- set for renditions: each receipt has its own
    owner             TEXT        NOT NULL DEFAULT 'store',  -- 'store' | 'operator' (an adopted dropoff file)
    display_path      TEXT        NOT NULL,  -- e.g. renditions/2026/10/2026-10-14__...pdf
    read_only_set     BOOLEAN     NOT NULL DEFAULT FALSE,
    last_verified_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_file_locations PRIMARY KEY (id),
    CONSTRAINT uq_file_locations_backend_locator UNIQUE (backend, locator),
    CONSTRAINT ck_file_locations_backend CHECK (backend IN ('drive', 'local')),
    CONSTRAINT ck_file_locations_purpose CHECK (purpose IN ('original', 'rendition')),
    CONSTRAINT ck_file_locations_owner CHECK (owner IN ('store', 'operator')),
    CONSTRAINT ck_file_locations_rendition_receipt
        CHECK ((purpose = 'rendition') = (receipt_id IS NOT NULL)),
    CONSTRAINT fk_file_locations_file FOREIGN KEY (file_id) REFERENCES receipt.stored_files (id),
    CONSTRAINT fk_file_locations_receipt FOREIGN KEY (receipt_id) REFERENCES receipt.receipts (id)
);
CREATE UNIQUE INDEX uq_file_locations_rendition_per_receipt
    ON receipt.file_locations (receipt_id, backend) WHERE purpose = 'rendition';

-- One row per source item: an email, a forward, a Drive scan.
CREATE TABLE receipt.provenance (
    id                          UUID        NOT NULL DEFAULT uuidv7(),
    receipt_id                  UUID        NOT NULL,
    is_primary                  BOOLEAN     NOT NULL DEFAULT FALSE,
    source_type                 TEXT        NOT NULL,       -- 'imap' | 'gdrive'
    source_name                 TEXT        NOT NULL,
    source_id                   TEXT        NOT NULL,       -- Message-ID or Drive file ID
    received_at                 TIMESTAMPTZ NOT NULL,
    email_subject               TEXT,
    email_sender                TEXT,
    email_date                  TIMESTAMPTZ,
    file_name                   TEXT,
    source_sha256               BYTEA,                      -- whole-original hash: the .eml, or Drive's sha256Checksum
    home_receipt_id             UUID,                       -- the receipt first created for this item, if any
    original_file_id            UUID,                       -- the .eml or the Drive file
    receipt_document_file_id    UUID,                       -- see "The receipt document"
    document_certainty          TEXT,                       -- 'certain' | 'ambiguous'
    original_status             TEXT        NOT NULL DEFAULT 'pending',
    original_unavailable_reason TEXT,
    match_method                TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_provenance PRIMARY KEY (id),
    CONSTRAINT uq_provenance_source_id UNIQUE (source_id),
    CONSTRAINT fk_provenance_receipt FOREIGN KEY (receipt_id) REFERENCES receipt.receipts (id),
    CONSTRAINT fk_provenance_home_receipt FOREIGN KEY (home_receipt_id) REFERENCES receipt.receipts (id),
    CONSTRAINT fk_provenance_original FOREIGN KEY (original_file_id) REFERENCES receipt.stored_files (id),
    CONSTRAINT fk_provenance_receipt_document FOREIGN KEY (receipt_document_file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT ck_provenance_original_status CHECK (original_status IN ('stored', 'pending', 'unavailable')),
    CONSTRAINT ck_provenance_document_certainty CHECK (document_certainty IN ('certain', 'ambiguous')),
    CONSTRAINT ck_provenance_match_method CHECK (match_method IN
        ('first', 'original_hash', 'document_hash', 'order_number', 'manual', 'migrated'))
);
CREATE UNIQUE INDEX uq_provenance_primary_per_receipt
    ON receipt.provenance (receipt_id) WHERE is_primary;
CREATE INDEX idx_provenance_receipt_id ON receipt.provenance (receipt_id);
CREATE INDEX idx_provenance_source_sha256 ON receipt.provenance (source_sha256);
CREATE INDEX idx_provenance_original_file_id ON receipt.provenance (original_file_id);
CREATE INDEX idx_provenance_receipt_document_file_id ON receipt.provenance (receipt_document_file_id);

-- Every file a source item brought with it.
CREATE TABLE receipt.provenance_files (
    provenance_id     UUID    NOT NULL,
    position          INTEGER NOT NULL,   -- 0 = the message or Drive file; 1.. = attachments in MIME order
    file_id           UUID    NOT NULL,
    role              TEXT    NOT NULL,   -- 'message' | 'attachment' | 'drive_original' | 'drive_export'
    file_name         TEXT,               -- the raw MIME filename, as sent
    CONSTRAINT pk_provenance_files PRIMARY KEY (provenance_id, position),
    CONSTRAINT fk_provenance_files_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id) ON DELETE CASCADE,
    CONSTRAINT fk_provenance_files_file FOREIGN KEY (file_id) REFERENCES receipt.stored_files (id),
    CONSTRAINT ck_provenance_files_role CHECK (role IN ('message', 'attachment', 'drive_original', 'drive_export'))
);

-- Suspected duplicates awaiting review (metadata-only matches, and hash matches a guard refused).
-- Pairs reference provenance, not receipts: provenance survives a link, so a decided pair
-- stays valid after a merge. Commands resolve current receipts through it.
CREATE TABLE receipt.duplicate_candidates (
    id                          UUID        NOT NULL DEFAULT uuidv7(),
    provenance_id               UUID        NOT NULL,
    candidate_of_provenance_id  UUID        NOT NULL,
    reason            TEXT        NOT NULL,   -- 'metadata' | 'document_hash_conflict' | 'order_number_conflict' | 'merge_blocked'
    status            TEXT        NOT NULL DEFAULT 'open',  -- 'open' | 'linked' | 'dismissed'
    decided_by        TEXT,
    decided_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_duplicate_candidates PRIMARY KEY (id),
    CONSTRAINT uq_duplicate_candidates_pair UNIQUE (provenance_id, candidate_of_provenance_id),
    CONSTRAINT fk_duplicate_candidates_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT fk_duplicate_candidates_candidate_of FOREIGN KEY (candidate_of_provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT ck_duplicate_candidates_status CHECK (status IN ('open', 'linked', 'dismissed'))
);

-- Append-only record of every link and unlink. Never updated or deleted.
CREATE TABLE receipt.provenance_link_history (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    provenance_id     UUID        NOT NULL,
    action            TEXT        NOT NULL,   -- 'link' | 'unlink' | 'merge' | 'unmerge'
    from_receipt_id   UUID,
    to_receipt_id     UUID        NOT NULL,
    match_method      TEXT        NOT NULL,
    actor             TEXT        NOT NULL,   -- 'ingest', or who ran the command
    actor_kind        TEXT        NOT NULL,   -- 'system' | 'person' | 'agent'
    reason            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_provenance_link_history PRIMARY KEY (id),
    CONSTRAINT fk_provenance_link_history_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT ck_provenance_link_history_action CHECK (action IN ('link', 'unlink', 'merge', 'unmerge')),
    CONSTRAINT ck_provenance_link_history_actor_kind CHECK (actor_kind IN ('system', 'person', 'agent'))
);

ALTER TABLE receipt.receipts
    ADD COLUMN rendition_file_id UUID,
    ADD COLUMN merged_into_id UUID,       -- set when a merge empties this receipt
    ADD COLUMN merged_at TIMESTAMPTZ,
    ADD CONSTRAINT fk_receipts_rendition_file FOREIGN KEY (rendition_file_id)
        REFERENCES receipt.stored_files (id),
    ADD CONSTRAINT fk_receipts_merged_into FOREIGN KEY (merged_into_id)
        REFERENCES receipt.receipts (id),
    ADD CONSTRAINT ck_receipts_merged_not_self CHECK (merged_into_id IS NULL OR merged_into_id <> id),
    ADD CONSTRAINT ck_receipts_merged_at CHECK ((merged_into_id IS NULL) = (merged_at IS NULL));
CREATE INDEX idx_receipts_merged_into_id ON receipt.receipts (merged_into_id);

-- Refuses every change. No cascade exception: nothing deletes provenance or receipts.
CREATE OR REPLACE FUNCTION receipt.forbid_link_history_change()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_provenance_link_history_append_only
    BEFORE UPDATE OR DELETE ON receipt.provenance_link_history
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_link_history_change();
CREATE OR REPLACE TRIGGER trg_provenance_link_history_no_truncate
    BEFORE TRUNCATE ON receipt.provenance_link_history
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_link_history_change();

-- merged_into_id never names a receipt that is itself merged, and a merged
-- receipt can't be merged into. A merge flattens chains (below) before setting it.
CREATE OR REPLACE FUNCTION receipt.check_merged_into()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.merged_into_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM receipt.receipts
        WHERE id = NEW.merged_into_id AND merged_into_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'receipt % would be merged into merged receipt %',
            NEW.id, NEW.merged_into_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_receipts_check_merged_into
    BEFORE INSERT OR UPDATE OF merged_into_id ON receipt.receipts
    FOR EACH ROW EXECUTE FUNCTION receipt.check_merged_into();
```

**Append-only, and what that guarantees.** Grants alone don't make `provenance_link_history` append-only in production, where the application connects as `receipt_index`, the owner of every table, and an owner can update and delete regardless of grants. The guard triggers above refuse UPDATE, DELETE and TRUNCATE for every role, the owner included. They use this design's own `receipt.forbid_link_history_change()`, which always refuses. The data-model design's `forbid_history_change()` reads `OLD.receipt_id` for its cascade exception, and this table has neither that column nor a cascade, so the two designs share no function. What isn't guaranteed: a superuser, or the owner disabling the triggers by hand, can still change the history. The triggers stop the application and mistakes, not an administrator.

**Migration sequence** (each step is its own migration file, so each can roll back alone):

1. Create `stored_files`, `file_locations`, `provenance` and `provenance_files`, and add `receipts.rendition_file_id` as nullable. `duplicate_candidates`, `provenance_link_history`, `receipts.merged_into_id` and `merged_at` and their triggers come with duplicate detection's own migration (ticket 7), as shaped above, by the operator's decision: nothing writes them before then. Drop NOT NULL from `receipts.pdf_path`, `receipts.source_id`, `receipts.source_name` (NOT NULL since receipt migration 000005) and `receipts.source_type` (NOT NULL since 000001), and drop `source_type`'s `'imap'` default, so a new receipt gets NULL rather than a wrong source type. `uq_receipts_source_id` stays, since it allows NULLs. From here on, receipts created by `unlink`, or by ingest after cutover to Drive, write NULL to the legacy columns, so neither the NOT NULL nor the UNIQUE constraint blocks them.
2. Backfill: one `provenance` row per existing receipt, copied from `receipts.source_id`, `source_type`, `source_name`, `file_name` and `email_*`, with `is_primary = true`, `home_receipt_id` set to that receipt, `match_method = 'migrated'` and `original_status = 'pending'`. The `received_at` comes from `email_date`, or `created_at` for Drive rows.
3. The code reads and writes provenance. Idempotency becomes the union of `provenance.source_id` and `ingest_log.source_id`, the same rule as today with `provenance` in place of `receipts`.
4. Set `rendition_file_id` NOT NULL. This is safe on every backend, because the schema ticket's local hash backfill already sets `rendition_file_id` for every existing receipt; on the production database it runs after the file migration ([section 11](#11-migrating-existing-files)) is verified.
5. After consumers have moved (one release later): drop `receipts.pdf_path`, `source_id`, `source_type`, `source_name`, `file_name` and `email_*`. The receipt data-model design builds its search document from provenance, so nothing it owns reads those columns.

### 7. Write Path

This is the interface agreed with the [always-on ingest design](always-on-ingest.md#7-dropoff-archive), with the operator's required fix to hash matching applied.

**What ingest calls:**

```python
def store_email_original(raw_bytes: bytes, *, source_name: str,
                         received_at: datetime) -> StoredEmail: ...
    # Stores message.eml and each attachment under one originals/ folder.
    # Returns the message's StoredFile and the attachments' StoredFiles, in MIME order.

def adopt_drive_original(file_id: str, *, source_name: str, received_at: datetime,
                         from_folder_id: str) -> StoredDriveOriginal: ...
    # Moves the dropoff file into originals/, sets it read-only, records it.
    # Native Google Docs: also stores a PDF export snapshot (purpose original, role drive_export).

@dataclass(frozen=True)
class StoredDriveOriginal:
    file: StoredFile             # the Drive file (for a native Doc, its drive_native row)
    export: StoredFile | None    # a native Doc's PDF export snapshot; None otherwise

@dataclass(frozen=True)
class StoredEmail:
    message: StoredFile          # message.eml
    attachments: list[StoredFile]  # in MIME order

def store_rendition(pdf: bytes, *, receipt_id: UUID, receipt_date: date, vendor: str,
                    amount: Decimal) -> StoredFile: ...
    # Uploads the receipt's own rendition file (step 3 of the order below). Its
    # file_locations row, tied to receipt_id, is written in step 5's transaction.
```

`RawReceipt` gains `raw_bytes`, the exact bytes of the whole original as fetched: the RFC 822 bytes the IMAP adapter parsed, so the stored original is byte for byte what extraction read, or a binary Drive file's bytes, whose sha256 is Drive's `sha256Checksum`. A native Google Doc has none (its download is an export).

**`adopt_drive_original` is idempotent**, keyed on the Drive file ID:

1. If `file_locations` already has `('drive', file_id)`, return that file.
2. Otherwise read the file's metadata. If its parent is already under `originals/`, a previous attempt moved it and crashed before recording. Set `contentRestrictions.readOnly` (again, if the crash fell between the move and the restriction), record it, and return.
3. Otherwise move it (`files.update` with `addParents` and `removeParents`, so the file ID stays the same), set `contentRestrictions.readOnly`, record it, and return.

**Order of one successful item:**

1. Fetch. Hash the whole original for `provenance.source_sha256`: the `.eml`'s sha256, or the Drive file's `sha256Checksum` (the sha256 of its bytes). For IMAP, store the original with `store_email_original` once the item is known to be kept: after rule 1 links it, or after extraction decides it isn't skipped. A skipped item stays at its source, so storing at fetch would leave every non-receipt email as an orphan (the operator's decision, ticket 3). Rule 1 needs only that hash, so it works whether or not the Drive file is ever adopted (the ingest design's `archive: false` default leaves Drive originals `pending`).
2. **Whole-original match before extraction.** If the whole original's hash links to an existing receipt under rule 1 ([section 8](#8-provenance-and-duplicates)), the item joins that receipt and the LLM call is skipped. Identical bytes mean an identical receipt, so the kind, vendor and amount are the same by construction. No other rule runs before extraction.
3. Otherwise extract, then apply rules 2 to 4, which need the extracted kind, vendor, paid amount and receipt document. If the item joins an existing receipt, it stores no rendition. Otherwise render, validate the PDF ([section 9](#9-pdf-validation-at-ingest)), and store the new receipt's rendition.
4. For a Drive source with `archive: true`, adopt the original. This is the same step as the ingest design's archive move. With `archive: false`, nothing moves and `original_status` stays `pending` until `backfill --originals-only` adopts it.
5. One transaction: the receipt (if new), the provenance row, its `provenance_files`, any order-number link or duplicate candidate, the link history row, and the `ingest_log` row.
6. After the commit, the IMAP tidy-up `MOVE` to the mail archive folder (owned by the ingest design).

**Failure modes:**

| Crash or failure after | State left | Recovery |
|------------------------|-----------|----------|
| An IMAP original was stored (step 1) | Files under `originals/` with no rows: the rows are recorded in step 5's transaction | The retry derives the same folder and names from the same bytes, so nothing is written again, and records them. An email with no Date header is dated at fetch, so a retry on a later day writes a second folder. `verify --orphans` reports any left for good |
| A rendition was uploaded (step 3) | A Drive rendition file with no location row | The retry uploads a fresh one (the receipt ID is new each attempt). `verify --orphans` reports the unrecorded file |
| A Drive adopt (step 4) | The file in `originals/` (or, under the copy fallback, copied, with the dropoff file in `processed/`), no receipt | The changes feed can't be relied on, since the file's parents no longer include the dropoff folder. The ingest design's retry scheduler requeues the item from its started record, finds the file by its ID, and the idempotent adopt returns the existing record before the commit |
| Upload checksum mismatch | Nothing recorded; the bad upload trashed | The item fails and is retried under the ingest design's [retry policy](always-on-ingest.md#6-retries) |
| The commit (step 5) | Nothing committed | The whole item is retried. Every earlier step is idempotent |

**Skipped and terminally failed items are not adopted.** They stay at the source, in the ingest design's `needs-attention` folder (IMAP folder or Drive subfolder), so the original is never lost. **Nothing deletes from `needs-attention` automatically.** A later `retry` that succeeds adopts the item then.

**Backfill** (the command belongs to the ingest design; see its [backfill section](always-on-ingest.md#8-backfill)) uses these from the store side:

```python
def provenance_missing_originals(source_name: str, since: date | None,
                                 until: date | None) -> list[Provenance]: ...
def attach_original(provenance_id: UUID, original: StoredEmail | StoredDriveOriginal) -> None: ...
def mark_original_unavailable(provenance_id: UUID, reason: str) -> None: ...
```

`backfill --originals-only` re-fetches each provenance row's item by its source ID (IMAP by Message-ID, across the watched folder and its archive; Drive by file ID), stores it, and attaches it with no re-extraction. It marks the row `unavailable`, with a reason, when the item is gone. It needs the provenance backfill (migration step 2) first.

### 8. Provenance and Duplicates

A receipt is one purchase (or one refund). A provenance record is one source item that evidences it. Search, totals and claim packets count receipts, so a duplicate that has been linked is never counted twice.

**Rules, in order.** Every rule requires both sides to have the same `kind`, so a refund never merges into its purchase. The receipt data-model design defines `kind` and refunds.

| # | Match | Result |
|---|-------|--------|
| 1 | **Whole original:** the new item's `source_sha256` (the `.eml`, the Drive file's checksum, or a native Doc's export) equals an existing provenance record's | Link (`original_hash`) |
| 2 | **Receipt document:** the new item's receipt document has the same sha256 as an existing provenance record's receipt document, *and* both are `certain` (see [The receipt document](#2-what-is-stored)), *and* the guard below passes | Link (`document_hash`) |
| 3 | **Order number:** the same order or invoice number, the same normalized vendor, the same paid amount (non-null and greater than zero on both sides), the same receipt date, and, where both receipts have line items, the same items | Link (`order_number`). If the order number, vendor and amount match but the date or the items differ, flag (`order_number_conflict`) and don't link |
| 4 | **Metadata only:** the same normalized vendor and paid amount, and dates within three days, with nothing above matching | Flag in `duplicate_candidates` (`metadata`). Never link |

**What rule 1 covers in practice.** A repeated email is caught before rule 1 by idempotency: `provenance.source_id` (the Message-ID) is unique across every source, as `receipts.source_id` is today, so the same message arriving in a second mailbox is skipped. Two different emails always differ in bytes, since each mail server adds its own headers. Rule 1 therefore links identical Drive files: the same scan dropped twice, with two file IDs. (A retried item never reaches it, because its crashed attempt left no provenance row.) A native Doc that isn't adopted has no export and no hash, so rule 1 skips it.

**Attachments other than the receipt document never match.** Unrelated receipts often share boilerplate attachments: a vendor's terms-and-conditions PDF, a logo, an inline image. Rule 2 compares receipt documents only, never "any attachment", and an ambiguous receipt document takes no part in it.

**The guard on rule 2.** Rule 2 links only when the new item's extracted kind, normalized vendor and paid amount all match the existing receipt. If the hashes match but any of those differ, the bytes aren't the receipt on their own: perhaps a generic invoice template, a refund that re-attaches the original invoice, or a mis-picked boilerplate PDF. The item becomes a new receipt, and the pair is flagged with `document_hash_conflict`.

**Why rule 3 checks the date and the items.** An order shipped in parts and charged once per shipment, in equal amounts, has one order number, one vendor and one amount, but two charges. A forward or a rescan of one charge has the same date and the same items. A split shipment almost always differs in one of them, so it is flagged for review, never linked. A null or zero paid amount (a receipt the extraction couldn't price, or a zero-amount receipt) never links by order number.

**Only rule 1 runs before extraction.** Rules 2 to 4 need the kind, vendor, paid amount, order number and receipt document that extraction produces, so they run after it, in step 3 of the write path. The extraction reports which attachment held the receipt (`receipt_document`), so rule 2 applies to an email whose receipt document is `certain`, as well as to Drive sources.

**Two real purchases stay two.** Two identical parking charges on one day match rule 4 and get flagged, not linked. They don't match rules 1 to 3, because they are two distinct emails, PDFs or order numbers.

**Undo and manual links:**

**A merge keeps the emptied receipt.** When a link moves a receipt's last provenance record to another receipt, the emptied receipt isn't deleted. **`merged_into_id` always names a receipt that isn't itself merged**, so the data-model design's `v_receipt_net` needs only one `COALESCE`. A link always targets the receipt that remains (a link to a merged receipt goes to the receipt it was merged into). When the receipt a merge empties already has receipts merged into it, the same transaction re-points them to the new target and records a `merge` for each. The `trg_receipts_check_merged_into` trigger refuses any write that would break this. It is marked `merged_into_id` (the receipt that remains) with `merged_at`, and the link history records a `merge`. Everything attached to it stays where it is: its extractions and events (the data-model design's `ON DELETE CASCADE` tables never fire, since nothing is deleted), its rendition and rendition location (for a back-catalogue receipt whose original is `unavailable`, that rendition is the only evidence), and its `refund_of`. Merged receipts are left out of search, totals and packets. The data-model design's `v_receipt_net` view counts a refund against the purchase that remains (`COALESCE(purchase.merged_into_id, purchase.id)`), and leaves a merged refund out, so net paid stays right without rewriting `refund_of`. `show` on a merged receipt says what it was merged into, and `show` on the receipt that remains lists what was merged into it.

- `receipt-index link <provenance-id> <receipt-id> --reason TEXT` merges by hand (`manual`). A merge is refused, and names what blocks it, when the receipt it would empty carries anything a person or agent set: tags, external links or hand-edited fields (the data-model design's records). Those would drop out of search with the merged receipt, so a person moves or removes them first. A refund pointing at it doesn't block, since the view follows the merge.
- `receipt-index unlink <provenance-id> --reason TEXT` moves a provenance record out to a receipt of its own. If the record has a `home_receipt_id` that was merged, that receipt is restored: `merged_into_id` cleared and an `unmerge` recorded, with its fields, extractions, events and rendition as they were. Otherwise (the item joined an existing receipt at ingest, so no receipt was ever created for it), a new receipt is created as a copy of the old receipt's fields, marked for re-extraction (the data-model design's `reextract`), with its own rendition location rendered from the moved provenance's original. Such an item always has a stored original, since rules 1 to 3 run at ingest, when the original is in hand. If the old receipt's primary moves, its oldest remaining provenance becomes primary.
- `receipt-index duplicates scan` runs rules 1 to 4, with the same guards and the same-kind rule, over receipts that already exist. Today's duplicates were ingested before any rule existed, so without a scan they would go on double-counting past periods. It runs once after the rendition hash backfill and the originals backfill, and again at will. Its links are recorded with actor `duplicates-scan`, kind `system`, and a reason. It applies the same refusal as `link`: a pair whose merge is blocked is flagged (`merge_blocked`) rather than linked.
- `receipt-index duplicates [--status open]` lists flagged pairs. `duplicates dismiss <id>` and `duplicates link <id>` decide them.
- Every link and unlink writes `provenance_link_history` with the actor, the actor kind (system, person or agent) and the reason. An agent may run `duplicates` and `unlink`, recorded as `agent`. Search and `show --output json` gain `provenance` (the list) and `suspected_duplicate_of` (open candidates), so a packet builder can see both.

### 9. PDF Validation at Ingest

Every PDF that enters the store (a PDF attachment, a Drive PDF, a rendition) is opened with **pikepdf** (qpdf), a new dependency, in strict mode: without recovery, saved again, and with no qpdf warnings. The warnings count because qpdf opens a PDF whose `%PDF-` header is damaged in strict mode, with only a warning.

- **`ok`:** it passes the strict check.
- **`repaired`:** it fails the strict check, but qpdf, with recovery, rewrites it into a file that passes. **The original is never changed.** The repaired bytes become the receipt's *rendition*, a new `stored_files` row with `repaired_from_id` pointing at the original.
- **`unparseable`:** anything else, including a PDF that needs a user password. The original is still stored (it is the evidence). If it was to be the rendition, the renderer falls back to rendering the email body. If there is no body, the item fails with a clear error and goes to the retry path.

**The PDF is untrusted, so qpdf and pdfplumber run in bounded child processes.** Saving a PDF decodes its streams, and reading its text decodes its page content. A file of a few kilobytes can inflate to gigabytes (a twice-deflated XMP `/Metadata` or content stream) or take minutes of CPU (a filter chain). Both jobs run through one runner, each in its own child, so a PDF too costly to read is still judged on its own bytes. The limits are kept per kind of document (`PdfBounds`), so a later kind such as statements gets its own rather than loosening these. They are there to stop malicious input. For receipts (`RECEIPT_BOUNDS`):

| Job | CPU | Wall clock | Also |
|---|---|---|---|
| `check` (qpdf) | 10 s | 30 s | |
| `read` (pdfplumber) | 15 s | 45 s | no text from a PDF over 50 pages, decided from the page count before any page is read |

Both run under a 512 MB address-space limit and refuse a file over 25 MB without opening it, all far below `item_timeout_seconds`. A check that hits a limit, is killed or times out makes the PDF `unparseable`, with the reason logged. A read that does gives no text, as does a PDF over the page cap. An email attachment is then left out, as an unreadable PDF is; a Drive PDF or a stored rendition falls back to vision. No PDF content is decoded in the ingest process. An ordinary receipt costs well under the limits: measured, a check takes about 0.15 s of the child's CPU, and a read 0.2 s for a text page and up to about 2.3 s for ten dense table pages. Most of a read's cost on a short PDF is the child's start and its pdfplumber import. A dense page costs pdfplumber 0.2 to 0.45 s of CPU, so a dense PDF under 50 pages can still pass the read's CPU limit and give no text; receipts are a page or two, so that is accepted. The read closes each page after taking its text, so memory grows only slightly with a PDF's length. A clean check result is cached by content, so an item's attachment and its pass-through rendition, the same bytes, are checked once. A `MemoryError` that does reach an item is `transient`.

PDFs from careless generators (a page missing `/Resources`, `/MediaBox` or `/Type`, or a `startxref` a byte off) fail the strict check and come out `repaired`: their rendition is qpdf's rewrite, and the original is kept as sent.

`pdf_status` is recorded on the stored file and describes that row's own bytes. A source that failed the strict check is `repaired`, and its repaired copy is `ok`, with `repaired_from_id` set, so every rendition is `ok`. A generated rendition (from HTML, text or an image) that isn't `ok` fails the item. Packet builders know what they are merging. Repairing at ingest moves the failure from packet-building time, when the original source may be long gone, to ingest time, where it is logged and retried. pdfplumber stays for text extraction.

Until Drive adopt is built, a Drive PDF enters the store only as a rendition. An unparseable one stores nothing and goes to the retry path. A repaired one's source bytes get a `stored_files` row with no location, so that `repaired_from_id` has a target; adopt reuses that row by sha256. Files stored before validation keep `pdf_status` NULL: rows are inserted whole and never updated. For the same reason, when two damaged copies repair to identical bytes, their receipts share one rendition row, and its `repaired_from_id` names the first source; each receipt's own source is its attachment, through `provenance_files`.

### 10. Reading

- **CLI.** `show` prints the rendition's reference and Drive link. `show --open` and a new `receipt-index file <ref> [--output PATH]` stream a file through `EvidenceStore.open()`. `search --output json` gains `rendition` (`ref`, `sha256`, `web_url`) and `provenance`, and keeps `pdf_path` for one release (filled in only for files that still have a local location).
- **The read-only MCP server and packet builders** read files through the same interface, by reference. They never build a path or a Drive URL themselves.
- **The operator** browses the evidence root in Drive as a Viewer.

### 11. Migrating Existing Files

`receipt-index store migrate --to drive [--limit N] [--dry-run]` is resumable and idempotent:

1. For each receipt with no Drive rendition location, read its local rendition (`pdf_path`). The schema ticket's hash backfill has already recorded its `stored_files` row, its `local` location and `receipts.rendition_file_id`.
2. Upload it to `renditions/YYYY/MM/` under its current name, and compare Drive's `sha256Checksum` with the recorded hash.
3. Record the receipt's `drive` rendition location.

**Verification, before the backend switches to Drive in config:**

- Every receipt has its own verified Drive rendition location (`file_locations` with its `receipt_id`, backend `drive`).
- The count of Drive rendition locations equals the receipt count. This holds even when two receipts share rendition bytes, because each has its own location.
- `verify --deep` passes over the whole store.

The command prints the counts. The runbook records them as the cutover's evidence.

**Back-catalogue originals** come from the ingest design's `backfill --originals-only`. It re-fetches emails still on the IMAP server (the watched folder and its archive) and files still in Drive. It drains the Drive dropoff into `originals/`, since back-catalogue scans still sit there today. Items that can no longer be found are marked `unavailable`, with a reason, and their rendition remains the only evidence. `receipt-index store report` prints how many receipts have stored, pending and unavailable originals.

**The local tree and its Dropbox mirror stay untouched** after migration, read-only, until the operator retires them. After cutover the local tree gains no new files.

### 12. Backups and the Missing Second Copy

- **Database:** the nightly `pg_dump` with seven-day rotation stays as it is. A monthly dump is added (`receipt_index-month-MM.dump`, twelve names, so the Dropbox mirror stays bounded), because the database now holds hashes, provenance and link history that the files alone can't rebuild. Both are mirrored to Dropbox by the existing `rclone copy` line.
- **Files:** after cutover, each file stored in Drive has **one copy**. A second copy (a replica on another provider, or a cold archive) is deferred to a future feature, by the operator's decision. `file_locations` already allows a second backend per file, so that feature adds rows and a backend, not a schema change. The gap is a recorded risk, below.

---

## Schema Split With the Receipt Data-Model Design

| This design owns | The receipt data-model design owns |
|------------------|------------------------------------|
| `stored_files`, `file_locations`, `provenance`, `provenance_files`, `duplicate_candidates`, `provenance_link_history` | The receipt's own fields: paid amount, `kind`, `refund_of`, order number, payment fields, field confidence, `search_document` |
| `receipts.rendition_file_id`, replacing `pdf_path` | Line items, tags and tag history, external links |
| Moving `source_*`, `file_name` and `email_*` off `receipts` onto `provenance` | `receipt.extractions` (extraction history), with an `input_file_id` referencing `stored_files`, so each run records which file it read |
| `receipts.merged_into_id` and `merged_at`, and what a merge does | Leaving merged receipts out of search, totals and `v_receipt_net`, and counting a refund against the purchase that remains |

**Dependencies on the data-model design.** It has agreed to provide these, and this design relies on them:
- the extraction's `receipt_document` field (ticket 2 there);
- `extractions.input_file_id` (the migration that lands second adds its foreign key to `stored_files`);
- leaving merged receipts out of search, totals and `v_receipt_net`.

Until the `receipt_document` field exists, rule 2 applies to Drive sources only.

Where they meet:

- **Text search reads the email subject from `provenance`.** The data-model design builds its search document from the subjects there.
- **Duplicate rules require the same `kind`.** Rule 3 reads the data-model design's order number and paid amount, and doesn't own them.
- **Which attachment held the receipt.** The data-model design's extraction schema reports it, and this design records it as the receipt document.

---

## Implementation Plan

Proposed tickets, to be filed only after the operator signs off this design. Each ticket lists the files it touches.

1. **Storage interface, config-selected backend, still local.** `EvidenceStore`, `StoredFile` and `LocalEvidenceStore` with the new folder layout; the `store.backend` config; the CLI no longer hardcodes the store. Files: `store.py`, `config.py`, `cli.py`, `pipeline.py`, tests, the CLI reference.
2. **Schema: stored files, locations, provenance; hash backfill.** Migration steps 1 to 3, a local hash backfill of existing renditions (recording each receipt's `stored_files` row, `local` rendition location and `rendition_file_id`), idempotency on `provenance.source_id`. Files: new migrations under `db/migrations/receipt/`, `tests/integration/conftest.py`, `repository.py`, `models.py`, `pipeline.py`.
3. **Keep originals (local backend).** `RawReceipt.raw_bytes`, `store_email_original`, receipt-document selection, `provenance_files`. Files: `models.py`, `adapters/imap.py`, `adapters/gdrive.py`, `store.py`, `pipeline.py`, `repository.py`.
4. **Drive backend, dedicated account and scope change.** `DriveEvidenceStore`, `drive_client.py` with its guard, checksum verification on upload, `adopt_drive_original`, read-only restrictions, `auth gdrive` with the `drive` scope, the setup runbook. **Its first step is the check in [Open Questions](#open-questions) item 1.** Files: new `drive_client.py`, `store.py`, `auth.py`, `adapters/gdrive.py`, `config.py`, `deploy/example.env`, `deploy/example.receipt-index.yaml`, `docs/user/server-deployment.md`, `docs/user/production-setup.md`.
5. **`verify` and `store report`, scheduled and alerting.** A daily `verify` and a monthly `verify --deep` in the service's schedule, each exiting non-zero on any mismatch, missing file or lifted restriction, and reported through the same channel as the always-on ingest design's health alerts. With no second copy, an unscheduled check would let a deleted original go unnoticed past Drive's 30-day trash. Files: `cli.py`, `store.py`, `repository.py`, `deploy/crontab` (or the service schedule that replaces it), `docs/user/server-deployment.md`, the CLI reference.
6. **Migrate the local tree to Drive.** `store migrate`, the cutover runbook, then migration step 4. Files: `cli.py`, `store.py`, a new migration, `docs/user/server-deployment.md`.
7. **Duplicate detection, link, unlink, duplicates.** A migration for `duplicate_candidates`, `provenance_link_history`, the merge columns and their triggers ([Schema](#6-schema)), and the merge conditions in `v_receipt_net`. Rules 1 to 4 with the guard, the commands, `duplicates scan` over existing receipts, and the JSON output fields. Rule 3 needs the data-model design's order number and paid amount. Files: new `duplicates.py`, `pipeline.py`, `repository.py`, `cli.py`, the CLI reference.
8. **PDF validation and repair.** pikepdf, `pdf_status`, repaired renditions. Files: `pyproject.toml`, new `pdf_validate.py`, `pipeline.py`, `renderer.py`, `originals.py`, `store.py`, `repository.py`, and a grants-only migration.
9. **Monthly database dump.** Files: `deploy/backup.sh`, `deploy/crontab`, `docs/user/server-deployment.md`.
10. **Drop the old columns** (migration step 5), one release after ticket 6. Files: a new migration, `repository.py`, `models.py`, `cli.py`.

**Order:** 1 → 2 → 3 → 4 → 5 → 6. Ticket 7 follows 3, plus the data-model design's order-number ticket for rule 3; rules 1 and 2 can ship without it. Ticket 8 can follow 1 at any point. Ticket 9 is independent. Ticket 10 comes last. The ingest design's backfill of originals follows ticket 3 (local) or 4 (Drive).

---

## Testing Strategy

### Unit Tests

- `put` idempotence: the same bytes twice give one row and no second write.
- `rename` refuses an original.
- Receipt-document selection: a PDF the extraction names (certain); a lone PDF the extraction doesn't name (ambiguous); several PDFs; body only; a logo-only image; a photographed receipt image the extraction names.
- An email with the receipt in its body and a lone terms-and-conditions PDF, sent twice with two amounts: not linked.
- A refund email that re-attaches its purchase's invoice PDF: not linked (rule 2 guard, `document_hash_conflict`).
- Attachment naming: a `../` filename, a filename with control characters, and a very long one, all stored under safe names inside the store root, with the raw names kept in `provenance_files`.
- Duplicate rules: each rule's positive and negative cases, including two identical parking charges (flagged, not linked), a shared terms-and-conditions PDF across two vendors (not linked), the rule 2 guard (`document_hash_conflict`), and a refund with the purchase's order number and amount (not linked).
- `link` and `unlink` round trip, including a primary that moves, with history rows written. A merge keeps the emptied receipt's extractions, events and rendition. `unlink` restores a merged receipt exactly, including one whose original is `unavailable`.
- A merge is refused when the emptied receipt has tags, links or hand edits, and `duplicates scan` flags that pair as `merge_blocked`.
- Merge chains: merging B into C when A is already merged into B leaves both A and B pointing at C, and a direct write of a `merged_into_id` naming a merged receipt is refused by the trigger.
- Rule 3: two equal charges on one order on different dates, and two with different line items, are flagged, not linked. A null or zero paid amount never links.
- The application role can't UPDATE, DELETE or TRUNCATE `provenance_link_history`, tested as the owning role, as in production.
- An email attachment whose bytes match an adopted (operator-owned) Drive file still gets its own store-owned copy.
- The Drive guard: one test per row of the guard table, plus the import check.
- PDF validation: a clean PDF, a truncated PDF and a PDF with a broken header that each repair, and an unparseable one, each built in the test from a small valid PDF, never a real document. Hostile PDFs, also built in the test: a memory bomb and a CPU bomb are `unparseable` to the check and give no text to the read, within the bound, with the ingest process's memory flat.

### Integration Tests (devtest Postgres)

- Each migration up and down, mirrored in `conftest.py`, and the provenance backfill against seeded receipts.
- An end-to-end email ingest on the local backend: the original, attachments and rendition stored, and hashes recorded.
- The same Drive PDF dropped twice (two file IDs, identical bytes) links by rule 1, with no LLM call.
- The same Message-ID in a second source is skipped by idempotency.
- Two receipts whose renditions have identical bytes each get their own rendition location, and renaming one leaves the other alone.
- `link` on a flagged pair deletes the emptied receipt, and the candidate row survives as `linked`.
- Two concurrent `put`s of the same bytes end with one `stored_files` row.

### Drive Backend

- Against a fake Drive client in CI: adopt idempotence across each crash point in the failure-modes table, the checksum mismatch path, and `verify` reporting a lifted restriction and a missing file.
- A manual, documented smoke test against a scratch folder of the dedicated account, run once by the Drive ticket and recorded in its pull request. CI never holds a Drive token.

Each new check is first seen to fail against a planted defect, per the project's practice.

---

## Risks & Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| **No second copy of files stored after cutover** (deferred by the operator's decision) | High: losing the Drive account or the evidence root loses originals that exist nowhere else | Low | Drive trash keeps deleted files for 30 days, which covers accidental deletion only, not a compromised token (below). The store account is used only by the service, and its token is guarded. Originals are read-only. Daily `verify` detects a missing file within a day. Email originals still exist in the mail archive folder until the operator deletes them. The pre-cutover local tree and its Dropbox mirror stay. A future feature adds the second copy, and `file_locations` is ready for it. Drive dropoff originals have the least cover: they are moved, not copied |
| A leaked store token | High: whoever holds it can permanently delete every file the store account owns (a permanent delete skips trash), empty the trash and lift read-only restrictions, so with no second copy it can destroy originals for good. The guard limits only this code, not the holder of the token | Low | The dedicated account sees only the evidence root and the folders shared with it, so the operator's own Drive stays out of reach. The token lives in the server `.env` like the other secrets. The setup runbook (Drive backend ticket) documents revoking the token and issuing a new one for the dedicated account. Daily `verify` reports missing originals and lifted restrictions within a day. The future second-copy feature is the real mitigation |
| The OAuth app left in Testing status | High: the refresh token expires after seven days and ingest stops | Medium | Setup step 2 checks it, and the always-on ingest design's health check alerts on stalled ingest |
| A false link merges two real purchases | High: an expense drops out of a claim | Low | Only exact-hash and order-number rules link, the rule 2 guard, same-`kind` on every rule, flags for metadata matches, and `unlink` with history |
| A missed duplicate | Medium: a total counts twice | Medium | Rule 4 flags it for review, and `duplicates` lists open pairs for the packet builder |
| Drive API quota or outages | Low: ingest delays, nothing lost | Low | The ingest design's retries, and items stay in the dropoff or the IMAP folder until they commit |
| Native Google Docs change after adoption | Low | Low | A read-only restriction on the Doc, and the export snapshot's hash is the evidence |

---

## Out of Scope and Future Work

- **A second copy of the evidence store**: a replica on another provider or a cold archive (Glacier-style). Deferred to a future feature by the operator's decision; see [Risks](#risks--mitigation).
- Retiring the pre-cutover local tree and its Dropbox mirror. The operator decides when.
- Tamper-proof, legally immutable storage (WORM). Read-only restrictions guard against accidents, not against the account owner.
- Fuzzy duplicate scoring beyond rule 4, and automatic linking on metadata.
- Drive push notifications (`changes.watch`). They belong to the always-on ingest design.
- Encryption at rest beyond what Drive provides.

---

## Open Questions

| # | Question | Who decides, and when |
|---|----------|-----------------------|
| 1 | **Can the store account move an operator-owned file out of the shared dropoff folder into a folder it owns, and set a read-only restriction on it?** Google's documentation doesn't say so clearly for My Drive. Even if it can, the moved file stays owned by the operator's account: it counts against the operator's quota, and the operator can still delete it. The fallback is for the store account to **copy** the file into `originals/` (a file it owns, verified by `sha256Checksum`) and move the dropoff file into the dropoff's own `processed/` subfolder, which leaves a second copy in the operator's Drive. **The fallback's contract:** adoption stays keyed on the *dropoff* file ID, which stays the provenance `source_id`; the copy's own file ID is the `drive` location's locator. Before copying, a retry looks in the target `originals/` folder for a file with the same `sha256Checksum` and records that copy rather than making another. The guard's two fallback rows (copy into `originals/`, move into `processed/`) are on only when the config selects the fallback. | The Drive backend ticket checks it against a scratch folder as its first step and reports the result on its issue. The operator then confirms the move as signed off, or chooses the copy fallback. The always-on ingest design's archive step follows whichever is chosen |
| 2 | The date window for rule 4 (three days proposed) | The operator, at the review of the duplicates ticket, with data from the first `duplicates scan` |
| 3 | **Is rule 3's same-date-and-items condition right?** It keeps equal split-shipment charges from linking, and also stops monthly charges that share a stable number (a permit, account or subscription number read as the order number) from linking. It narrows a rule the operator settled, so the operator confirms it | The operator, at the review of the duplicates ticket, once the data-model design's extraction shows how well it tells order numbers from account numbers |
| 4 | When to retire the local tree and its Dropbox mirror | The operator, after the second-copy feature lands |
