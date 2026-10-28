-- receipt.forbid_history_change() stays: 000011 owns it, and extractions
-- still uses it. Dropping the columns also drops their constraints, indexes
-- and column-level grants.
DROP TRIGGER IF EXISTS trg_receipt_events_no_truncate ON receipt.receipt_events;
DROP TRIGGER IF EXISTS trg_receipt_events_append_only ON receipt.receipt_events;
DROP TABLE IF EXISTS receipt.receipt_events;

DROP INDEX IF EXISTS receipt.idx_receipts_payment_last4;
DROP INDEX IF EXISTS receipt.idx_receipts_order_number;

ALTER TABLE receipt.receipts
    DROP CONSTRAINT IF EXISTS ck_receipts_payment_last4,
    DROP CONSTRAINT IF EXISTS ck_receipts_payment_method,
    DROP COLUMN IF EXISTS payer,
    DROP COLUMN IF EXISTS payment_last4,
    DROP COLUMN IF EXISTS payment_method,
    DROP COLUMN IF EXISTS order_number;
