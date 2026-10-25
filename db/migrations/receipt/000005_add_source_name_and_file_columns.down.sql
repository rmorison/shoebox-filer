ALTER TABLE receipt.receipts
    DROP COLUMN IF EXISTS source_name,
    DROP COLUMN IF EXISTS file_name;
