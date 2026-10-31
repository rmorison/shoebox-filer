-- Lossy once duplicates have been linked: rolling back drops the link
-- history and the merge columns, so every merged receipt counts again as a
-- live receipt in search and totals, and rolling forward doesn't restore the
-- merges. Unlink the merged receipts first if they must stay merged.

-- Restore 000016's view first: it reads merged_into_id.
CREATE OR REPLACE VIEW receipt.v_receipt_net AS
WITH linked_refunds AS (
    SELECT r.id,
           COALESCE(r.paid_amount, r.amount) AS refund_amount,
           r.refund_of                       AS purchase_id
    FROM receipt.receipts r
    WHERE r.kind = 'refund' AND r.refund_of IS NOT NULL
)
SELECT p.id                                   AS receipt_id,
       COALESCE(p.paid_amount, p.amount)      AS paid,
       COALESCE(SUM(lr.refund_amount), 0)     AS refunded,
       COALESCE(p.paid_amount, p.amount)
         - COALESCE(SUM(lr.refund_amount), 0) AS net_paid,
       COUNT(lr.id)                           AS refund_count
FROM receipt.receipts p
LEFT JOIN linked_refunds lr ON lr.purchase_id = p.id
WHERE p.kind = 'purchase'
GROUP BY p.id;

DROP TRIGGER IF EXISTS trg_receipts_check_merged_into ON receipt.receipts;
DROP FUNCTION IF EXISTS receipt.check_merged_into();

-- Dropping the table drops its triggers.
DROP TABLE IF EXISTS receipt.provenance_link_history;
DROP FUNCTION IF EXISTS receipt.forbid_link_history_change();
DROP TABLE IF EXISTS receipt.duplicate_candidates;

-- Dropping the columns also drops their constraints, index and grants.
ALTER TABLE receipt.receipts
    DROP CONSTRAINT IF EXISTS ck_receipts_merged_at,
    DROP CONSTRAINT IF EXISTS ck_receipts_merged_not_self,
    DROP CONSTRAINT IF EXISTS fk_receipts_merged_into,
    DROP COLUMN IF EXISTS merged_at,
    DROP COLUMN IF EXISTS merged_into_id;

-- 000017 and 000019 granted no UPDATE on provenance; take back this one's.
REVOKE UPDATE (receipt_id, is_primary, home_receipt_id, match_method)
    ON receipt.provenance FROM receipt_index_dev_write;
