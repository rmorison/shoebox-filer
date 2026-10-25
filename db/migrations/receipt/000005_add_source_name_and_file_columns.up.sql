ALTER TABLE receipt.receipts
    ADD COLUMN source_name TEXT,
    ADD COLUMN file_name TEXT;

-- Backfill existing rows with a placeholder name
UPDATE receipt.receipts SET source_name = 'legacy-imap' WHERE source_name IS NULL;

-- Enforce NOT NULL after backfill
ALTER TABLE receipt.receipts ALTER COLUMN source_name SET NOT NULL;

-- Grant write access to new columns
GRANT INSERT (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT UPDATE (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_write;

-- Grant read access to new columns
GRANT SELECT (source_name, file_name)
    ON receipt.receipts TO receipt_index_dev_read;
