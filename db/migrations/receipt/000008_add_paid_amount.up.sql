-- Paid amount and per-field confidence (receipt data-model design, §1).
-- amount keeps its meaning: the order, invoice or statement total.
-- paid_amount is what was charged (or, for a refund, returned); NULL means
-- not known, 0 means nothing was charged.
ALTER TABLE receipt.receipts
    ADD COLUMN paid_amount      NUMERIC(12,2),
    ADD COLUMN field_confidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD CONSTRAINT ck_receipts_paid_amount_non_negative
        CHECK (paid_amount IS NULL OR paid_amount >= 0);

-- Search filters on the effective amount: paid when known, else the total.
CREATE INDEX IF NOT EXISTS idx_receipts_effective_amount
    ON receipt.receipts ((COALESCE(paid_amount, amount)));

ALTER TABLE receipt.ingest_log
    ADD COLUMN paid_amount NUMERIC(12,2);

-- Grant access to new columns
GRANT INSERT (paid_amount, field_confidence), UPDATE (paid_amount, field_confidence)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT SELECT (paid_amount, field_confidence)
    ON receipt.receipts TO receipt_index_dev_read;
GRANT INSERT (paid_amount) ON receipt.ingest_log TO receipt_index_dev_write;
GRANT SELECT (paid_amount) ON receipt.ingest_log TO receipt_index_dev_read;
