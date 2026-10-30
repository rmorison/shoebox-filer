-- Restoring the NOT NULLs fails if a receipt holds NULL in a legacy column.
-- Nothing writes NULL to them before cutover to the Drive store.
ALTER TABLE receipt.extractions
    DROP CONSTRAINT IF EXISTS fk_extractions_input_file;

ALTER TABLE receipt.receipts
    ALTER COLUMN source_type SET DEFAULT 'imap',
    ALTER COLUMN source_type SET NOT NULL,
    ALTER COLUMN source_name SET NOT NULL,
    ALTER COLUMN source_id SET NOT NULL,
    ALTER COLUMN pdf_path SET NOT NULL;

-- Dropping the column drops its key, index and column-level grants.
ALTER TABLE receipt.receipts DROP COLUMN IF EXISTS rendition_file_id;

DROP TABLE IF EXISTS receipt.provenance_files;
DROP TABLE IF EXISTS receipt.provenance;
DROP TABLE IF EXISTS receipt.file_locations;
DROP TABLE IF EXISTS receipt.stored_files;
