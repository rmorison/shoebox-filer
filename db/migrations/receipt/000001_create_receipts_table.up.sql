CREATE SCHEMA IF NOT EXISTS receipt;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- uuidv7() is a built-in function in PostgreSQL 18+ (no extension required).
-- See: https://www.postgresql.org/docs/18/functions-uuid.html
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

CREATE INDEX IF NOT EXISTS idx_receipts_vendor
    ON receipt.receipts USING gin (vendor gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_receipts_amount
    ON receipt.receipts (amount);
CREATE INDEX IF NOT EXISTS idx_receipts_receipt_date
    ON receipt.receipts (receipt_date);

CREATE OR REPLACE TRIGGER trg_receipts_set_updated_at
    BEFORE UPDATE ON receipt.receipts
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();
