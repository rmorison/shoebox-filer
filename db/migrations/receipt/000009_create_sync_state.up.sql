-- Each source's read position: one row per (source, folder).
-- Design: docs/engineering/designs/always-on-ingest.md, section 5 (ADR-0008).
-- Sync state is a cache of where to start reading. Deleting a row is safe:
-- the source re-bootstraps and the processed-ID check absorbs the re-read.

CREATE TABLE IF NOT EXISTS receipt.sync_state (
    id                 UUID NOT NULL DEFAULT uuidv7(),
    source_name        TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    folder             TEXT NOT NULL,
    imap_uidvalidity   BIGINT,
    imap_last_uid      BIGINT,
    drive_page_token   TEXT,
    drive_account_id   TEXT,
    archive_enabled_at TIMESTAMPTZ,
    last_poll_at       TIMESTAMPTZ,
    last_success_at    TIMESTAMPTZ,
    last_error         TEXT,
    last_error_at      TIMESTAMPTZ,
    resets             INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_sync_state PRIMARY KEY (id),
    CONSTRAINT uq_sync_state_source_folder UNIQUE (source_name, folder),
    CONSTRAINT ck_sync_state_source_type CHECK (source_type IN ('imap', 'gdrive')),
    CONSTRAINT ck_sync_state_imap_cursor CHECK (
        source_type <> 'imap' OR (drive_page_token IS NULL AND drive_account_id IS NULL)),
    CONSTRAINT ck_sync_state_drive_cursor CHECK (
        source_type <> 'gdrive' OR (imap_uidvalidity IS NULL AND imap_last_uid IS NULL))
);

CREATE OR REPLACE TRIGGER trg_sync_state_set_updated_at
    BEFORE UPDATE ON receipt.sync_state
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.sync_state TO receipt_index_dev_all;

-- _write: read and advance cursors (no DELETE; resetting a source is an operator step)
GRANT SELECT, INSERT, UPDATE ON receipt.sync_state TO receipt_index_dev_write;

-- _read: SELECT only
GRANT SELECT ON receipt.sync_state TO receipt_index_dev_read;
