-- Refunds, credits and statements (receipt data-model design, §6, widened by
-- the operator: kind takes purchase, refund, credit or statement, and
-- signed_amount makes a plain sum the net spend).
--
-- A refund is its own receipt, with positive amounts; kind gives the
-- direction. refund_of links a refund to the purchase it returns money for.
-- receipt_net gives each purchase its net paid after linked refunds.

ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS kind      TEXT NOT NULL DEFAULT 'purchase',
    ADD COLUMN IF NOT EXISTS refund_of UUID;

ALTER TABLE receipt.receipts
    ADD CONSTRAINT ck_receipts_kind
        CHECK (kind IN ('purchase', 'refund', 'credit', 'statement')),
    ADD CONSTRAINT fk_receipts_refund_of FOREIGN KEY (refund_of)
        REFERENCES receipt.receipts (id) ON DELETE SET NULL,
    ADD CONSTRAINT ck_receipts_refund_of_kind
        CHECK (refund_of IS NULL OR kind = 'refund'),
    ADD CONSTRAINT ck_receipts_refund_of_not_self
        CHECK (refund_of IS NULL OR refund_of <> id);

-- The effective amount with the sign a total needs: a purchase counts for, a
-- refund or credit against, a statement not at all (its transactions are
-- charged or receipted elsewhere). Generated, so it can't fall out of step
-- with kind or the amounts. Receipt.signed_amount computes the same in Python.
-- "0 - x" rather than "-x" keeps a refund of 0.00 from reading -0.00.
ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS signed_amount NUMERIC(12,2) GENERATED ALWAYS AS (
        CASE kind
            WHEN 'purchase' THEN COALESCE(paid_amount, amount)
            WHEN 'statement' THEN 0
            ELSE 0 - COALESCE(paid_amount, amount)
        END
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_receipts_refund_of
    ON receipt.receipts (refund_of);

-- Net paid is computed, not stored, so it can't fall out of step with the
-- refunds. The merge conditions of the design's view (merged_into_id) wait
-- for the evidence-store merge, which adds them.
CREATE OR REPLACE VIEW receipt.receipt_net AS
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

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.receipt_net TO receipt_index_dev_all;

-- _write: kind and refund_of are written; signed_amount is generated and
-- takes no INSERT or UPDATE grant.
GRANT INSERT (kind, refund_of), UPDATE (kind, refund_of)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT SELECT (kind, refund_of, signed_amount)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT SELECT ON receipt.receipt_net TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup and the MCP server read these)
GRANT SELECT (kind, refund_of, signed_amount)
    ON receipt.receipts TO receipt_index_dev_read;
GRANT SELECT ON receipt.receipt_net TO receipt_index_dev_read;
