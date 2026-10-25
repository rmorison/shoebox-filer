ALTER TABLE receipt.receipts
    DROP CONSTRAINT ck_receipts_amount_non_negative,
    ADD CONSTRAINT ck_receipts_amount_positive CHECK (amount > 0);
