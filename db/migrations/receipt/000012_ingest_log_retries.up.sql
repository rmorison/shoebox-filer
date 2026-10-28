-- Retries and failure surfacing (always-on ingest design, section 6).
-- ingest_log stays append-only: the latest row for a source_id gives the
-- item's state. A failed row carries its attempt number, whether it used up
-- an attempt, when it is due again (NULL: terminal) and its error class.
-- `retry --yes` requeues an item by writing a 'queued' row that points at
-- the retry_requests row recording who asked, when, with which filters.

CREATE TABLE IF NOT EXISTS receipt.retry_requests (
    id            UUID NOT NULL DEFAULT uuidv7(),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    requested_by  TEXT NOT NULL,
    db_user       TEXT NOT NULL DEFAULT current_user,
    filters       JSONB NOT NULL,
    matched       INTEGER NOT NULL,
    requeued      INTEGER NOT NULL,

    CONSTRAINT pk_retry_requests PRIMARY KEY (id),
    CONSTRAINT ck_retry_requests_counts
        CHECK (matched >= 0 AND requeued >= 0 AND requeued <= matched)
);

ALTER TABLE receipt.ingest_log
    ADD COLUMN attempt          INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN counts_attempt   BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN next_retry_at    TIMESTAMPTZ,
    ADD COLUMN error_class      TEXT,
    ADD COLUMN stored_ref       TEXT,
    ADD COLUMN retry_request_id UUID;

ALTER TABLE receipt.ingest_log DROP CONSTRAINT ck_ingest_log_status;
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_status
    CHECK (status IN ('started', 'success', 'failed', 'skipped', 'queued'));
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_error_class
    CHECK (error_class IN ('transient', 'content', 'config'));
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_attempt
    CHECK (attempt >= 0);
ALTER TABLE receipt.ingest_log ADD CONSTRAINT fk_ingest_log_retry_request
    FOREIGN KEY (retry_request_id) REFERENCES receipt.retry_requests (id);

CREATE INDEX IF NOT EXISTS idx_ingest_log_next_retry_at
    ON receipt.ingest_log (next_retry_at) WHERE next_retry_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingest_log_source_id_latest
    ON receipt.ingest_log (source_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_log_retry_request_id
    ON receipt.ingest_log (retry_request_id) WHERE retry_request_id IS NOT NULL;

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.retry_requests TO receipt_index_dev_all;

-- _write: 000004 grants INSERT on ingest_log column by column
GRANT INSERT (attempt, counts_attempt, next_retry_at, error_class, stored_ref,
              retry_request_id)
    ON receipt.ingest_log TO receipt_index_dev_write;
-- Column by column, so requested_at and db_user keep their defaults. requeued
-- is written once the requeue loop knows how many items it reached.
GRANT SELECT ON receipt.retry_requests TO receipt_index_dev_write;
GRANT INSERT (requested_by, filters, matched, requeued)
    ON receipt.retry_requests TO receipt_index_dev_write;
GRANT UPDATE (requeued) ON receipt.retry_requests TO receipt_index_dev_write;

-- _read: SELECT only
GRANT SELECT ON receipt.retry_requests TO receipt_index_dev_read;
