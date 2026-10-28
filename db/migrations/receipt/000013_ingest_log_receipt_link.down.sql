-- Dropping the column also drops its constraint and its
-- column-level grant. Link-only rows stay 'skipped' rows without their link.
ALTER TABLE receipt.ingest_log
    DROP CONSTRAINT IF EXISTS ck_ingest_log_receipt_link,
    DROP COLUMN IF EXISTS receipt_link;
