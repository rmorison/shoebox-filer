ALTER TABLE receipt.receipts
    DROP CONSTRAINT ck_receipts_amount_positive,
    ADD CONSTRAINT ck_receipts_amount_non_negative CHECK (amount >= 0);
