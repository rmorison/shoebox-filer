-- Dropping the columns also drops their check constraint and column grants.
DROP INDEX IF EXISTS receipt.idx_receipts_effective_amount;

ALTER TABLE receipt.receipts
    DROP COLUMN IF EXISTS paid_amount,
    DROP COLUMN IF EXISTS field_confidence;

ALTER TABLE receipt.ingest_log
    DROP COLUMN IF EXISTS paid_amount;
