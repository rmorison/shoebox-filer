-- Dropping the columns also drops their constraints, index and column-level
-- grants. The view reads the columns, so it goes first.
DROP VIEW IF EXISTS receipt.receipt_net;

DROP INDEX IF EXISTS receipt.idx_receipts_refund_of;

ALTER TABLE receipt.receipts
    DROP COLUMN IF EXISTS signed_amount,
    DROP CONSTRAINT IF EXISTS ck_receipts_refund_of_not_self,
    DROP CONSTRAINT IF EXISTS ck_receipts_refund_of_kind,
    DROP CONSTRAINT IF EXISTS fk_receipts_refund_of,
    DROP CONSTRAINT IF EXISTS ck_receipts_kind,
    DROP COLUMN IF EXISTS refund_of,
    DROP COLUMN IF EXISTS kind;
