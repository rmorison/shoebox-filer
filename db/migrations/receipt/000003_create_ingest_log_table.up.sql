CREATE TABLE IF NOT EXISTS receipt.ingest_log (
    id              UUID NOT NULL DEFAULT uuidv7(),
    source_id       TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'imap',
    status          TEXT NOT NULL,
    receipt_id      UUID,
    vendor          TEXT,
    amount          NUMERIC(12,2),
    email_subject   TEXT,
    email_sender    TEXT,
    email_date      TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_ingest_log PRIMARY KEY (id),
    CONSTRAINT ck_ingest_log_status CHECK (status IN ('success', 'failed', 'skipped')),
    CONSTRAINT fk_ingest_log_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_log_status
    ON receipt.ingest_log (status);
CREATE INDEX IF NOT EXISTS idx_ingest_log_source_id
    ON receipt.ingest_log (source_id);
CREATE INDEX IF NOT EXISTS idx_ingest_log_created_at
    ON receipt.ingest_log (created_at);
