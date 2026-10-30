-- Stored files, their locations and provenance (evidence-store design, §6,
-- migration step 1). duplicate_candidates, provenance_link_history and the
-- merge columns come with duplicate detection's own migration.

-- One row per distinct content. The same bytes stored twice are one row.
CREATE TABLE IF NOT EXISTS receipt.stored_files (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    content_kind      TEXT        NOT NULL DEFAULT 'bytes',
    sha256            BYTEA,
    byte_size         BIGINT,
    media_type        TEXT        NOT NULL,
    pdf_status        TEXT,
    repaired_from_id  UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_stored_files PRIMARY KEY (id),
    CONSTRAINT uq_stored_files_sha256 UNIQUE (sha256),
    CONSTRAINT ck_stored_files_content_kind
        CHECK (content_kind IN ('bytes', 'drive_native')),
    CONSTRAINT ck_stored_files_sha256_present
        CHECK (content_kind = 'drive_native'
               OR (sha256 IS NOT NULL AND octet_length(sha256) = 32)),
    CONSTRAINT ck_stored_files_pdf_status
        CHECK (pdf_status IN ('ok', 'repaired', 'unparseable')),
    CONSTRAINT fk_stored_files_repaired_from FOREIGN KEY (repaired_from_id)
        REFERENCES receipt.stored_files (id)
);

-- Where each stored file lives. Each receipt has its own rendition location.
CREATE TABLE IF NOT EXISTS receipt.file_locations (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    file_id           UUID        NOT NULL,
    backend           TEXT        NOT NULL,
    locator           TEXT        NOT NULL,
    purpose           TEXT        NOT NULL,
    receipt_id        UUID,
    owner             TEXT        NOT NULL DEFAULT 'store',
    display_path      TEXT        NOT NULL,
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
    CONSTRAINT fk_file_locations_file FOREIGN KEY (file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT fk_file_locations_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_locations_rendition_per_receipt
    ON receipt.file_locations (receipt_id, backend) WHERE purpose = 'rendition';
CREATE INDEX IF NOT EXISTS idx_file_locations_file_id
    ON receipt.file_locations (file_id);

-- One row per source item: an email, a forward, a Drive scan.
CREATE TABLE IF NOT EXISTS receipt.provenance (
    id                          UUID        NOT NULL DEFAULT uuidv7(),
    receipt_id                  UUID        NOT NULL,
    is_primary                  BOOLEAN     NOT NULL DEFAULT FALSE,
    source_type                 TEXT        NOT NULL,
    source_name                 TEXT        NOT NULL,
    source_id                   TEXT        NOT NULL,
    received_at                 TIMESTAMPTZ NOT NULL,
    email_subject               TEXT,
    email_sender                TEXT,
    email_date                  TIMESTAMPTZ,
    file_name                   TEXT,
    source_sha256               BYTEA,
    home_receipt_id             UUID,
    original_file_id            UUID,
    receipt_document_file_id    UUID,
    document_certainty          TEXT,
    original_status             TEXT        NOT NULL DEFAULT 'pending',
    original_unavailable_reason TEXT,
    match_method                TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_provenance PRIMARY KEY (id),
    CONSTRAINT uq_provenance_source_id UNIQUE (source_id),
    CONSTRAINT fk_provenance_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id),
    CONSTRAINT fk_provenance_home_receipt FOREIGN KEY (home_receipt_id)
        REFERENCES receipt.receipts (id),
    CONSTRAINT fk_provenance_original FOREIGN KEY (original_file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT fk_provenance_receipt_document FOREIGN KEY (receipt_document_file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT ck_provenance_original_status
        CHECK (original_status IN ('stored', 'pending', 'unavailable')),
    CONSTRAINT ck_provenance_document_certainty
        CHECK (document_certainty IN ('certain', 'ambiguous')),
    CONSTRAINT ck_provenance_match_method CHECK (match_method IN
        ('first', 'original_hash', 'document_hash', 'order_number', 'manual', 'migrated'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_provenance_primary_per_receipt
    ON receipt.provenance (receipt_id) WHERE is_primary;
CREATE INDEX IF NOT EXISTS idx_provenance_receipt_id
    ON receipt.provenance (receipt_id);
CREATE INDEX IF NOT EXISTS idx_provenance_source_sha256
    ON receipt.provenance (source_sha256);
CREATE INDEX IF NOT EXISTS idx_provenance_original_file_id
    ON receipt.provenance (original_file_id);
CREATE INDEX IF NOT EXISTS idx_provenance_receipt_document_file_id
    ON receipt.provenance (receipt_document_file_id);

-- Every file a source item brought with it.
CREATE TABLE IF NOT EXISTS receipt.provenance_files (
    provenance_id     UUID    NOT NULL,
    position          INTEGER NOT NULL,
    file_id           UUID    NOT NULL,
    role              TEXT    NOT NULL,
    file_name         TEXT,

    CONSTRAINT pk_provenance_files PRIMARY KEY (provenance_id, position),
    CONSTRAINT fk_provenance_files_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id) ON DELETE CASCADE,
    CONSTRAINT fk_provenance_files_file FOREIGN KEY (file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT ck_provenance_files_role
        CHECK (role IN ('message', 'attachment', 'drive_original', 'drive_export'))
);

CREATE INDEX IF NOT EXISTS idx_provenance_files_file_id
    ON receipt.provenance_files (file_id);

CREATE OR REPLACE TRIGGER trg_stored_files_set_updated_at
    BEFORE UPDATE ON receipt.stored_files
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE OR REPLACE TRIGGER trg_file_locations_set_updated_at
    BEFORE UPDATE ON receipt.file_locations
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE OR REPLACE TRIGGER trg_provenance_set_updated_at
    BEFORE UPDATE ON receipt.provenance
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- The receipt's rendition, replacing pdf_path. Nullable until migration step 4.
ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS rendition_file_id UUID;
ALTER TABLE receipt.receipts
    ADD CONSTRAINT fk_receipts_rendition_file FOREIGN KEY (rendition_file_id)
        REFERENCES receipt.stored_files (id);
CREATE INDEX IF NOT EXISTS idx_receipts_rendition_file_id
    ON receipt.receipts (rendition_file_id);

-- The legacy source columns move to provenance. A receipt created after
-- cutover writes NULL to them; uq_receipts_source_id stays, as it allows NULLs.
ALTER TABLE receipt.receipts
    ALTER COLUMN pdf_path DROP NOT NULL,
    ALTER COLUMN source_id DROP NOT NULL,
    ALTER COLUMN source_name DROP NOT NULL,
    ALTER COLUMN source_type DROP NOT NULL,
    ALTER COLUMN source_type DROP DEFAULT;

-- 000011 left input_file_id without its key; this migration lands second.
ALTER TABLE receipt.extractions
    ADD CONSTRAINT fk_extractions_input_file FOREIGN KEY (input_file_id)
        REFERENCES receipt.stored_files (id);

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.stored_files, receipt.file_locations,
    receipt.provenance, receipt.provenance_files TO receipt_index_dev_all;

-- _write: read, and insert only the columns ingest and the hash backfill
-- write. Nothing writes provenance_files, content_kind, owner or
-- source_sha256 yet; the tickets that write them, and any that update these
-- tables, add their grants.
GRANT SELECT ON receipt.stored_files, receipt.file_locations,
    receipt.provenance, receipt.provenance_files TO receipt_index_dev_write;
GRANT INSERT (sha256, byte_size, media_type)
    ON receipt.stored_files TO receipt_index_dev_write;
GRANT INSERT (file_id, backend, locator, purpose, receipt_id, display_path)
    ON receipt.file_locations TO receipt_index_dev_write;
GRANT INSERT (receipt_id, is_primary, source_type, source_name, source_id,
              received_at, email_subject, email_sender, email_date, file_name,
              home_receipt_id, original_status, match_method)
    ON receipt.provenance TO receipt_index_dev_write;
GRANT UPDATE (rendition_file_id) ON receipt.receipts TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup reads every table). It already
-- reads every column of receipts (000002).
GRANT SELECT ON receipt.stored_files, receipt.file_locations,
    receipt.provenance, receipt.provenance_files TO receipt_index_dev_read;
